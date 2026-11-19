"""Observe transitional HTTP statuses instead of failing the whole page attempt."""

import os
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import cast
from unittest import TestCase
from unittest.mock import patch

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Route

from app.collection.browser import (
    CollectionError,
    CollectionStopped,
    browser_page,
    read_page,
    warm_profile,
)
from app.collection.captcha import (
    DRAG_STEPS,
    BrokerSolver,
    CaptchaRequest,
    Challenge,
    ChallengePending,
    Drag,
    GiveUp,
    apply_answer,
)
from app.collection.collectors import cian

HOST = "https://collector.test"
START = HOST + "/listing"

_TRANSITION = """<meta charset="utf-8">
    <h1>Доступ ограничен: проверка безопасности</h1>
    <script>
        setTimeout(() => {
            document.body.innerHTML = '<div id="listing">' + Array.from(
                {length: 3}, (_, i) => '<a href="/card_' + i + '">card</a>').join('')
                + '</div>';
            window.__ready = true;
        }, 250);
    </script>"""

_STUB = """<meta charset="utf-8">
    <h1>Доступ ограничен: проверка безопасности</h1>"""


def _capture(page: Page) -> Callable[[datetime, float], str]:
    def capture(_observed_at: datetime, deadline: float) -> str:
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        page.wait_for_function("() => window.__ready === true", timeout=remaining)
        return str(
            page.evaluate("() => document.querySelectorAll('#listing a').length")
        )

    return capture


def _request(root: Path, start: tuple[float, float] = (10.0, 20.0)) -> CaptchaRequest:
    return CaptchaRequest(
        identifier="probe01",
        challenge=Challenge("cian", "drag", "servicepipe"),
        url="https://collector.test/listing",
        screenshot=root / "pending" / "probe01.png",
        start=start,
        created_at=datetime.now(UTC),
    )


class TransitionalStatusTests(TestCase):
    def test_transitional_status_is_observed_until_the_document_is_ready(self) -> None:
        # Arrange: replay a resolved challenge — the body becomes a listing after 250 ms.
        navigations: list[str] = []

        def serve(request: Route) -> None:
            navigations.append(request.request.url)
            request.fulfill(
                status=439, content_type="text/html; charset=utf-8", body=_TRANSITION
            )

        # Act
        with browser_page() as page:
            page.route("**/*", serve)
            started = time.monotonic()
            result = read_page(
                page,
                START,
                lambda: False,
                _capture(page),
                accepts_redirect=lambda loaded: loaded == START,
            )
            elapsed = time.monotonic() - started

        # Assert: one navigation, the transitional status did not fail the attempt.
        self.assertEqual(result, "3")
        self.assertEqual(navigations, [START])
        self.assertGreaterEqual(elapsed, 0.2)

    def test_unresolved_transition_fails_with_its_own_reason_and_one_navigation(
        self,
    ) -> None:
        # Arrange: the challenge never resolves inside the observation budget.
        navigations: list[str] = []

        def serve(request: Route) -> None:
            navigations.append(request.request.url)
            request.fulfill(
                status=439, content_type="text/html; charset=utf-8", body=_STUB
            )

        # Act
        with browser_page() as page:
            page.route("**/*", serve)
            started = time.monotonic()
            with self.assertRaisesRegex(CollectionError, "^http_439$"):
                read_page(
                    page,
                    START,
                    lambda: False,
                    _capture(page),
                    accepts_redirect=lambda loaded: loaded == START,
                    challenge_budget=0.3,
                )
            elapsed = time.monotonic() - started

        # Assert: the reason names the status, and the document is not re-navigated.
        self.assertEqual(navigations, [START])
        self.assertGreaterEqual(elapsed, 0.3)

    def test_rate_limit_pauses_and_repeats_the_document_once(self) -> None:
        # Arrange: first navigation is rejected with 429, the second is complete.
        navigations: list[str] = []

        def serve(request: Route) -> None:
            navigations.append(request.request.url)
            if len(navigations) == 1:
                request.fulfill(
                    status=429, content_type="text/html; charset=utf-8", body=_STUB
                )
            else:
                request.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=_TRANSITION,
                )

        # Act
        with browser_page() as page:
            page.route("**/*", serve)
            started = time.monotonic()
            result = read_page(
                page,
                START,
                lambda: False,
                _capture(page),
                accepts_redirect=lambda loaded: loaded == START,
                retry_pause=0.05,
            )
            elapsed = time.monotonic() - started

        # Assert: exactly one repeat, after the pause.
        self.assertEqual(result, "3")
        self.assertEqual(navigations, [START, START])
        self.assertGreaterEqual(elapsed, 0.05)

    def test_ready_document_is_not_delayed_by_observation(self) -> None:
        # Arrange: an ordinary 200 page that becomes ready in 250 ms.
        navigations: list[str] = []

        def serve(request: Route) -> None:
            navigations.append(request.request.url)
            request.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=_TRANSITION
            )

        # Act
        with browser_page() as page:
            page.route("**/*", serve)
            started = time.monotonic()
            result = read_page(
                page,
                START,
                lambda: False,
                _capture(page),
                accepts_redirect=lambda loaded: loaded == START,
            )
            elapsed = time.monotonic() - started

        # Assert: success is not held for the challenge budget.
        self.assertEqual(result, "3")
        self.assertEqual(navigations, [START])
        self.assertLess(elapsed, 2)

    def test_collector_challenge_is_reported_as_its_own_reason(self) -> None:
        # Arrange: the collector sees a check and reports it instead of a refusal.
        navigations: list[str] = []

        def serve(request: Route) -> None:
            navigations.append(request.request.url)
            request.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=_STUB,
            )

        def challenge(_observed_at: datetime, _deadline: float) -> str:
            raise ChallengePending(Challenge("cian", "drag", "servicepipe"))

        # Act / Assert
        with browser_page() as page:
            page.route("**/*", serve)
            with self.assertRaisesRegex(CollectionError, "^source_captcha$"):
                read_page(
                    page,
                    START,
                    lambda: False,
                    challenge,
                    accepts_redirect=lambda loaded: loaded == START,
                )

        self.assertEqual(navigations, [START])

    def test_stop_requested_during_a_challenge_takes_precedence(self) -> None:
        # Arrange: an operator stops the run while the collector reports a check.
        stopped = Event()

        def challenge(_observed_at: datetime, _deadline: float) -> str:
            stopped.set()
            raise ChallengePending(Challenge("cian", "drag", "servicepipe"))

        def serve(request: Route) -> None:
            request.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=_STUB
            )

        # Act / Assert
        with browser_page() as page:
            page.route("**/*", serve)
            with self.assertRaises(CollectionStopped):
                read_page(
                    page,
                    START,
                    stopped.is_set,
                    challenge,
                    accepts_redirect=lambda loaded: loaded == START,
                )

    def test_cian_collector_reports_the_servicepipe_marker(self) -> None:
        # Arrange: a card document carrying the recorded challenge marker.
        url = "https://samara.cian.ru/sale/flat/330631159/"
        body = '<meta charset="utf-8"><title>Циан</title><div id="js-challenge-loader"></div>'

        def serve(request: Route) -> None:
            request.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=body
            )

        # Act / Assert
        with browser_page() as page:
            page.route("**/*", serve)
            page.goto(url)
            with self.assertRaises(ChallengePending):
                cian.capture_card(page, url, datetime.now(UTC), time.monotonic() + 5)

    def test_broker_reads_the_answer_written_next_to_the_request(self) -> None:
        # Arrange: an operator writes the answer beside the published request.
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        request = _request(root)
        (root / "answers").mkdir()
        (root / "answers" / f"{request.identifier}.json").write_text(
            '{"action": "drag", "dx": 120, "dy": 0}', encoding="utf-8"
        )

        # Act
        actions = BrokerSolver(root, wait=1.0)(request, lambda: False)

        # Assert
        self.assertEqual(actions, (Drag(120.0, 0.0),))

    def test_broker_gives_up_on_a_missing_or_broken_answer(self) -> None:
        # Arrange
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        request = _request(root)
        (root / "answers").mkdir()
        (root / "answers" / f"{request.identifier}.json").write_text(
            "{not json", encoding="utf-8"
        )

        # Act
        actions = BrokerSolver(root, wait=0.1)(request, lambda: False)

        # Assert
        self.assertEqual(actions, (GiveUp(),))

    def test_broker_stops_waiting_when_the_run_is_stopped(self) -> None:
        # Arrange
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        request = _request(root)
        stopped = Event()
        stopped.set()

        # Act
        started = time.monotonic()
        actions = BrokerSolver(root, wait=30.0)(request, stopped.is_set)
        elapsed = time.monotonic() - started

        # Assert
        self.assertEqual(actions, (GiveUp(),))
        self.assertLess(elapsed, 1)

    def test_drag_moves_the_pointer_in_ordered_steps(self) -> None:
        # Arrange: a card-sized document that records every pointer move.
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        request = _request(root, start=(50.0, 60.0))

        with browser_page() as page:
            page.set_content(
                "<script>window.moves = [];"
                "document.addEventListener('pointermove',"
                " event => window.moves.push([event.clientX, event.clientY]));</script>"
            )

            # Act
            applied = apply_answer(page, request, (Drag(120.0, 0.0),))
            moves = cast(list[list[float]], page.evaluate("() => window.moves"))

        # Assert: the pointer walks the track instead of jumping to the end.
        self.assertTrue(applied)
        xs = [move[0] for move in moves]
        self.assertGreaterEqual(len(xs), DRAG_STEPS)
        self.assertEqual(xs, sorted(xs))
        self.assertAlmostEqual(xs[-1], 170.0, delta=3)

    def test_give_up_applies_nothing(self) -> None:
        # Arrange
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        request = _request(root)

        with browser_page() as page:
            page.set_content("<p>заглушка</p>")

            # Act
            applied = apply_answer(page, request, (GiveUp(),))

        # Assert
        self.assertFalse(applied)

    def test_ephemeral_context_does_not_create_a_profile(self) -> None:
        # Arrange: no profile name means the previous behaviour.
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = {
            "COLLECTION_PROFILE": "",
            "COLLECTION_PROFILE_ROOT": str(root),
        }

        # Act
        with (
            patch.dict(os.environ, environment),
            browser_page() as page,
        ):
            page.set_content("<p>одноразовый запуск</p>")

        # Assert
        self.assertFalse((root / "cian").exists())

    def test_proxy_setting_reaches_the_browser_context(self) -> None:
        # Arrange: an unreachable proxy must fail the navigation, not be ignored.
        environment = {
            "COLLECTION_PROXY": "socks5://127.0.0.1:1",
            "COLLECTION_PROFILE": "",
        }

        # Act / Assert
        with patch.dict(os.environ, environment), browser_page() as page:
            with self.assertRaises(PlaywrightError):
                page.goto(START, timeout=5000)

    def test_warm_profile_is_skipped_without_a_named_profile(self) -> None:
        # Arrange: without a profile there is nothing to warm.
        visited: list[str] = []

        def serve(request: Route) -> None:
            visited.append(request.request.url)
            request.fulfill(status=200, content_type="text/html", body="<p>сайт</p>")

        # Act
        with (
            patch.dict(os.environ, {"COLLECTION_PROFILE": ""}),
            browser_page() as page,
        ):
            page.route("**/*", serve)
            warm_profile(page, "https://collector.test/deep/listing", lambda: False)

        # Assert
        self.assertEqual(visited, [])
