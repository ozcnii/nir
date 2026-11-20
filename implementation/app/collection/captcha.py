"""Ask for and apply one answer to a detected access challenge."""

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

CaptchaKind = Literal["click", "drag", "unknown"]

CAPTCHA_WAIT_S = 120.0
POLL_INTERVAL_S = 0.3
DRAG_STEPS = 12
MAX_PAUSE_S = 30.0
IMAGE_SETTLE_MS = 8_000.0
NEW_IMAGE_MS = 10_000.0
_CHALLENGE_NODE = (
    '#js-challenge-loader, script[src*="servicepipe"], [data-captcha], iframe'
)


@dataclass(frozen=True)
class Challenge:
    """One access check found on the currently loaded document."""

    source: str
    kind: CaptchaKind
    marker: str
    # CAPTCHA-DOCUMENT: a check that replaced the requested document is safe to
    # read again; a check shown over a document the site already served is not.
    replaces_document: bool = False


class ChallengePending(Exception):
    """The document is an access check; the attempt is not decided yet.

    Deliberately not a `RejectedCard` and not a `PlaywrightError`: the page
    attempt owns the decision, and the collectors only report what they saw.
    """

    def __init__(self, challenge: Challenge) -> None:
        super().__init__(f"{challenge.source}_challenge")
        self.challenge = challenge


@dataclass(frozen=True)
class Click:
    x: float
    y: float


@dataclass(frozen=True)
class Drag:
    dx: float
    dy: float
    # CAPTCHA-GRAB-POINT: a solver that sees the handle can name where to grab,
    # otherwise the drag starts at the published challenge centre.
    start: tuple[float, float] | None = None


@dataclass(frozen=True)
class Pause:
    """Wait before the next action, so a two-step check can settle."""

    seconds: float


@dataclass(frozen=True)
class GiveUp:
    """The check stays unresolved; the page attempt keeps its own reason."""


@dataclass(frozen=True)
class NoAnswer:
    """Nobody answered before the deadline; the document stays unverified."""


@dataclass(frozen=True)
class Done:
    """The answerer passed the check by hand; the document is read again."""


Action = Click | Drag | Pause | GiveUp | NoAnswer | Done


@dataclass(frozen=True)
class CaptchaRequest:
    identifier: str
    challenge: Challenge
    url: str
    screenshot: Path
    start: tuple[float, float]
    created_at: datetime


Solver = Callable[[CaptchaRequest, Callable[[], bool]], tuple[Action, ...]]


def unresolved(
    _request: CaptchaRequest, _stopped: Callable[[], bool]
) -> tuple[Action, ...]:
    """Default solver: no answer is available for this run."""
    return (GiveUp(),)


def _challenge_centre(page: Page) -> tuple[float, float]:
    observed = cast(
        dict[str, float],
        page.evaluate(
            """(selector) => {
                const node = document.querySelector(selector);
                const rect = node && node.getBoundingClientRect();
                return rect && rect.width > 0 && rect.height > 0
                    ? {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2}
                    : {x: window.innerWidth / 2, y: window.innerHeight / 2};
            }""",
            _CHALLENGE_NODE,
        ),
    )
    return float(observed["x"]), float(observed["y"])


def _wait_for_challenge_image(page: Page) -> None:
    """Give the task picture a chance to load before taking the screenshot."""
    try:
        page.wait_for_function(
            "() => Array.from(document.images).some(image => image.naturalWidth > 0)",
            timeout=IMAGE_SETTLE_MS,
        )
    except PlaywrightError:
        pass


def loaded_images(page: Page) -> frozenset[str]:
    """The pictures this document already shows, used as a stage baseline."""
    try:
        sources = cast(
            list[str],
            page.evaluate(
                """() => Array.from(document.images)
                    .filter(image => image.naturalWidth > 0)
                    .map(image => image.currentSrc || image.src)"""
            ),
        )
    except PlaywrightError:
        return frozenset()
    return frozenset(sources)


def _wait_for_new_image(page: Page, known: frozenset[str]) -> None:
    """A stage opened by our answer shows a picture this document did not have.

    CAPTCHA-NEW-STAGE: the refusal notice carries its own pictures, so "some
    image is loaded" is true long before the puzzle appears. Waiting for a new
    picture keeps the screenshot on the stage the site actually opened.
    """
    try:
        page.wait_for_function(
            """(known) => Array.from(document.images).some(
                image => image.naturalWidth > 0
                    && !known.includes(image.currentSrc || image.src))""",
            arg=sorted(known),
            timeout=NEW_IMAGE_MS,
        )
    except PlaywrightError:
        pass


_FRAME_TASK = (
    '[class*="geetest_slider"], [class*="geetest_holder"], [class*="geetest_panel"],'
    ' iframe[src*="frame=challenge"], iframe[src*="puzzle"], iframe[src*="geetest"]'
)


def _wait_for_frame_task(page: Page) -> bool:
    """Задача может жить во фрейме: без видимого фрейма снимок будет пустым."""
    try:
        page.wait_for_function(
            """(selector) => Array.from(document.querySelectorAll(selector))
                .some(node => {
                    const rect = node.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0
                        && rect.bottom > 0 && rect.top < innerHeight;
                })""",
            arg=_FRAME_TASK,
            timeout=IMAGE_SETTLE_MS,
        )
    except PlaywrightError:
        return False
    return True


def prepare_challenge(
    page: Page,
    pending: ChallengePending,
    root: Path,
    *,
    reload: bool,
    wait_for_new: frozenset[str] | None = None,
) -> CaptchaRequest:
    """Publish the check as the page shows it; the caller owns media and reload.

    CAPTCHA-KEEP-OPEN: a second stage opens inside the same document, over the
    refusal notice. Reloading there would close it and start the check over, so
    only the caller decides whether the document itself must be read again.
    """
    identifier = uuid.uuid4().hex[:12]
    pending_dir = root / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    screenshot = pending_dir / f"{identifier}.png"
    if reload:
        page.reload(wait_until="domcontentloaded")
    # Задача во фрейме не видна документу, поэтому ждём именно фрейм.
    if not _wait_for_frame_task(page):
        if wait_for_new is None:
            _wait_for_challenge_image(page)
        else:
            _wait_for_new_image(page, wait_for_new)
    page.screenshot(path=str(screenshot))
    start = _challenge_centre(page)

    request = CaptchaRequest(
        identifier=identifier,
        challenge=pending.challenge,
        url=page.url,
        screenshot=screenshot,
        start=start,
        created_at=datetime.now(UTC),
    )
    payload = {
        "id": identifier,
        "source": pending.challenge.source,
        "kind": pending.challenge.kind,
        "marker": pending.challenge.marker,
        "url": request.url,
        "screenshot": screenshot.name,
        "start": list(start),
        "created_at": request.created_at.isoformat(),
    }
    # Что именно показала площадка: разметка нужна, чтобы понять, во что
    # целиться, не гадая по картинке.
    try:
        observed = page.evaluate(
            """() => ({
                iframes: Array.from(document.querySelectorAll('iframe'))
                    .map(node => node.src).slice(0, 12),
                scripts: Array.from(document.querySelectorAll('script[src]'))
                    .map(node => node.src)
                    .filter(src => /captcha|geetest|qrator|servicepipe/i.test(src)),
                dialogs: Array.from(document.querySelectorAll('div[role="dialog"], [class*="modal"]'))
                    .map(node => (node.outerHTML || '').slice(0, 2000)).slice(0, 3),
            })"""
        )
        (pending_dir / f"{identifier}.dom.json").write_text(
            json.dumps(observed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except PlaywrightError:
        pass
    (pending_dir / f"{identifier}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return request


class BrokerSolver:
    """Read the answer an operator (or an agent) writes next to the request."""

    def __init__(
        self, root: Path, wait: float = CAPTCHA_WAIT_S, *, handover: bool = False
    ) -> None:
        self._root = root
        self._wait = wait
        # CAPTCHA-HANDOVER: a person watching the window keeps the page as it
        # is; an agent reading a screenshot may need the document re-read.
        self.handover = handover

    def __call__(
        self, request: CaptchaRequest, stopped: Callable[[], bool]
    ) -> tuple[Action, ...]:
        answer = self._root / "answers" / f"{request.identifier}.json"
        deadline = time.monotonic() + self._wait
        while time.monotonic() < deadline:
            if stopped():
                return (GiveUp(),)
            if answer.exists():
                return _read_answer(answer, request)
            time.sleep(POLL_INTERVAL_S)
        return (NoAnswer(),)


def _one_action(item: object, *, allow_done: bool = False) -> Action | None:
    if not isinstance(item, dict):
        return None
    entry = cast(dict[str, object], item)
    try:
        if entry.get("action") == "click":
            return Click(float(cast(float, entry["x"])), float(cast(float, entry["y"])))
        if entry.get("action") == "drag":
            point = entry.get("start")
            start = (
                (float(cast(list[float], point)[0]), float(cast(list[float], point)[1]))
                if isinstance(point, list) and len(point) == 2
                else None
            )
            return Drag(
                float(cast(float, entry["dx"])),
                float(cast(float, entry.get("dy", 0.0))),
                start,
            )
        if entry.get("action") == "pause":
            seconds = float(cast(float, entry["seconds"]))
            return Pause(seconds) if seconds >= 0 else None
        if allow_done and entry.get("action") == "done":
            return Done()
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _read_answer(path: Path, _request: CaptchaRequest) -> tuple[Action, ...]:
    # CAPTCHA-SEQUENCE: one check can need several steps, for example a block
    # notice whose button opens the slider. Both a single action and a list are
    # accepted, so a short answer stays short.
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return (GiveUp(),)
    if not isinstance(payload, dict):
        return (GiveUp(),)
    listed = payload.get("actions")
    if "actions" in payload and not isinstance(listed, list):
        return (GiveUp(),)
    # CAPTCHA-DONE: a handover confirmation stands alone; inside a sequence it
    # would be one step among others, so it is refused there.
    if isinstance(listed, list):
        items: list[object] = list(cast(list[object], listed))
        allow_done = False
    else:
        items = [payload]
        allow_done = True
    actions: list[Action] = []
    for item in items:
        action = _one_action(item, allow_done=allow_done)
        if action is None:
            return (GiveUp(),)
        actions.append(action)
    return tuple(actions) if actions else (GiveUp(),)


def apply_answer(
    page: Page, request: CaptchaRequest, actions: tuple[Action, ...]
) -> bool:
    """Perform the answered actions on the same document the request described."""
    applied = False
    for action in actions:
        if isinstance(action, (GiveUp, NoAnswer)):
            return False
        if isinstance(action, Click):
            page.mouse.move(action.x, action.y, steps=8)
            page.mouse.click(action.x, action.y)
            applied = True
        elif isinstance(action, Pause):
            time.sleep(min(action.seconds, MAX_PAUSE_S))
        elif isinstance(action, Drag):
            x, y = action.start if action.start is not None else request.start
            page.mouse.move(x, y, steps=8)
            page.mouse.down()
            for step in range(1, DRAG_STEPS + 1):
                page.mouse.move(
                    x + action.dx * step / DRAG_STEPS,
                    y + action.dy * step / DRAG_STEPS,
                )
            page.mouse.up()
            applied = True
    return applied
