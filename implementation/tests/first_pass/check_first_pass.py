"""Exercise the application path via a real launcher and app role."""

import json
import os
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import psycopg
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Route

from collection.one_card import CardReadFailed, collect_cian_card
from collection.runner import BrowserWork, run_with_browser
from collection.selection import Direction, Selection, parse
from collection.tests.test_cian import projection, script
from config.control import CONTROL_PATH, ControlError, decode, request
from config.journal import JOURNAL_ROOT
from config.launcher import serve
from config.run_events import RUN_ID_ENV
from config.startup import preflight, web_command

URL = "https://samara.cian.ru/sale/flat/330631159/"
REJECT_NEXT = CONTROL_PATH.parent / "reject-next"
DIRECTION = Direction("cian", 68, "new_build", "sale")
START: dict[str, object] = {
    "action": "start",
    "selection": {
        "sources": ["cian"],
        "regions": [68],
        "categories": ["new_build"],
        "deal_types": ["sale"],
    },
}


def proof_path(run_id: str) -> Path:
    return CONTROL_PATH.parent / f"first-pass-{run_id}.json"


def route_fixture(work: BrowserWork) -> list[tuple[str, datetime]]:
    url, offer, product = projection()
    assert url == URL
    if REJECT_NEXT.exists():
        offer["description"] = "reject\x00this"
    html = (
        f"<script>{script(offer)}</script>"
        f'<script type="application/ld+json">{json.dumps(product)}</script>'
        '<img src="/first-pass-probe.jpg">'
    )
    requested: list[tuple[str, datetime]] = []

    def serve_card(route: Route) -> None:
        requested.append((route.request.url, datetime.now(UTC)))
        if route.request.url == URL:
            if len(requested) == 1:
                route.fulfill(status=500, body="Controlled transient response")
                return
            route.fulfill(status=200, content_type="text/html", body=html)
        else:
            route.abort()

    work.pages.card.context.route("**/*", serve_card)
    return requested


def run_child(mode: str, incoming: str) -> int:
    def body(work: BrowserWork, _selection: Selection) -> None:
        intercepted = route_fixture(work) if mode == "fixture" else None
        path = proof_path(os.environ[RUN_ID_ENV])
        try:
            snapshot, receipt = collect_cian_card(work, URL)
        except CardReadFailed as error:
            observation: dict[str, object] = {
                "http": error.http_status,
                "problem": error.problem,
            }
            if mode == "live":
                page = work.pages.card
                with suppress(PlaywrightError):
                    observation["page_title"] = page.title()[:120]
                    observation["embedded_marker"] = any(
                        "frontend-offer-card" in text
                        for text in page.locator(
                            "script:not([src])"
                        ).all_text_contents()
                    )
            path.write_text(json.dumps(observation), encoding="utf-8")
            raise
        if intercepted is not None:
            assert [url for url, _ in intercepted] == [URL, URL]
            assert intercepted[0][1] <= snapshot["observed_at"] <= intercepted[1][1]
        price = snapshot["price"]
        photos = snapshot["photos"]
        path.write_text(
            json.dumps(
                {
                    "external_id": snapshot["external_id"],
                    "observed_at": snapshot["observed_at"].isoformat(),
                    "area_m2": str(snapshot["area_m2"]),
                    "price_rub": str(price["price_rub"])
                    if price["status"] == "known"
                    else None,
                    "price_kind": price["price_kind"]
                    if price["status"] == "known"
                    else None,
                    "photos": photos["urls"] if photos["status"] == "known" else None,
                    "ad_id": receipt.ad_id,
                    "property_id": receipt.property_id,
                    "outcome": receipt.outcome,
                }
            ),
            encoding="utf-8",
        )

    return run_with_browser(
        parse(decode(incoming.encode())),
        supported=lambda direction: direction == DIRECTION,
        body=body,
    )


def status(state: str, timeout: float = 90) -> dict[str, object]:
    end = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < end:
        try:
            last = request({"action": "status"})
        except ControlError as error:
            if not isinstance(error.__cause__, OSError):
                raise
        if last.get("state") == state:
            return last
        if last.get("state") == "unknown":
            break
        time.sleep(0.05)
    raise AssertionError(f"Expected {state}; last control state: {last}")


def one(expected_failure: str | None = None) -> dict[str, object]:
    launched = request(START)
    assert launched["ok"] is True
    run_id = cast(str, launched["run_id"])
    ended = status("finished")
    assert ended["run_id"] == run_id and ended["cleanup_confirmed"] is True
    raw = cast(
        dict[str, object], json.loads(proof_path(run_id).read_text(encoding="utf-8"))
    )
    if expected_failure is not None:
        assert raw.get("problem") == expected_failure
        assert ended["reason"] == "browser_operation_failed"
        saved = json.loads(
            (JOURNAL_ROOT / run_id / "state.json").read_text(encoding="utf-8")
        )
        assert saved["outcome"] == "error" and saved["progress"] == {
            "committed_writes": 0
        }
        return raw
    if "problem" in raw:
        print(
            json.dumps({"kind": "card_unavailable", **raw, "cleanup_confirmed": True}),
            flush=True,
        )
        raise SystemExit(20)
    assert ended["reason"] == "completed", ended
    return raw


def verify(mode: str) -> int:
    # Arrange: real app credentials and a new, manually driven launcher.
    ready = status("idle", timeout=25)
    assert ready["run_id"] is None
    with psycopg.connect(
        dbname=os.environ["DB_NAME"],
        user="app",
        password=os.environ["DB_APP_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        autocommit=True,
    ) as connection:
        assert connection.execute(
            "SELECT current_database(), current_user, session_user, current_setting('server_version_num')"
        ).fetchone() == ("t16_first_pass", "app", "app", "170012")
        assert connection.execute("SELECT count(*) FROM ads").fetchone() == (0,)

        # Act: two real manual control runs against one publication.
        first = one()
        second = one()
        if mode == "fixture":
            # The third, synthetic run provokes rejection before a committed write.
            REJECT_NEXT.touch()
            one(expected_failure="catalog_rejected")
        rows = connection.execute(
            """
            SELECT ads.id, property_id, external_id, observed_at, photos, area_m2
            FROM ads JOIN properties ON properties.id=ads.property_id
            """
        ).fetchall()
        history = connection.execute(
            "SELECT price_rub, price_kind FROM price_history ORDER BY observed_at"
        ).fetchall()

        # Assert: compare the actual parsed field values with persisted facts.
        assert first["outcome"] == "created", first["outcome"]
        assert second["outcome"] in ("unchanged", "updated"), second["outcome"]
        assert first["ad_id"] == second["ad_id"]
        assert first["property_id"] == second["property_id"]
        first_time = datetime.fromisoformat(cast(str, first["observed_at"]))
        second_time = datetime.fromisoformat(cast(str, second["observed_at"]))
        assert second_time > first_time
        assert isinstance(first["photos"], list) and first["photos"]
        assert isinstance(second["photos"], list) and second["photos"]
        assert rows == [
            (
                second["ad_id"],
                second["property_id"],
                second["external_id"],
                second_time,
                second["photos"],
                Decimal(cast(str, first["area_m2"])),
            )
        ]
        prices = [
            (Decimal(cast(str, value["price_rub"])), value["price_kind"])
            for value in (first, second)
        ]
        expected_history = prices[:1] if prices[0] == prices[1] else prices
        assert history == expected_history
        print(
            json.dumps(
                {
                    "mode": mode,
                    "key": second["external_id"],
                    "ad_id": second["ad_id"],
                    "property_id": second["property_id"],
                    "area_m2": first["area_m2"],
                    "observed_at": [first["observed_at"], second["observed_at"]],
                    "price_rub": [first["price_rub"], second["price_rub"]],
                    "price_kind": [first["price_kind"], second["price_kind"]],
                    "photo_urls": len(second["photos"]),
                    "history_rows": len(history),
                    "outcomes": [first["outcome"], second["outcome"]],
                    "app": "current_user=session_user=app",
                    "cleanup_confirmed": True,
                }
            ),
            flush=True,
        )
    return 0


def main() -> int:
    command, mode = sys.argv[1:3]
    if mode not in ("fixture", "live"):
        raise SystemExit("Expected fixture or live")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if command == "launch":
        if os.getuid() != 10001 or not preflight():
            return 1
        return serve(
            CONTROL_PATH, web_command(), [sys.executable, __file__, "child", mode]
        )
    if command == "child":
        return run_child(mode, sys.argv[3])
    if command == "verify":
        return verify(mode)
    raise SystemExit("Unsupported fixture command")


if __name__ == "__main__":
    raise SystemExit(main())
