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
from playwright.sync_api import Page, Route

CaptchaKind = Literal["click", "drag", "unknown"]

CAPTCHA_WAIT_S = 120.0
POLL_INTERVAL_S = 0.3
DRAG_STEPS = 12
MAX_PAUSE_S = 30.0
IMAGE_SETTLE_MS = 8_000.0
_CHALLENGE_NODE = (
    '#js-challenge-loader, script[src*="servicepipe"], [data-captcha], iframe'
)


@dataclass(frozen=True)
class Challenge:
    """One access check found on the currently loaded document."""

    source: str
    kind: CaptchaKind
    marker: str


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


Action = Click | Drag | Pause | GiveUp


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


_OPEN_CHALLENGE = r"""() => {
    // A second stage is only "open" when its dialog is really visible: an
    // element carrying the class name may be hidden in the base markup.
    const shown = (node) => {
        if (!node) return false;
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return rect.width > 100 && rect.height > 100
            && style.visibility !== "hidden" && style.display !== "none";
    };
    return Array.from(document.querySelectorAll('iframe[src*="geetest"]')).some(shown);
}"""


def _challenge_on_screen(page: Page) -> bool:
    """Whether the check is already shown and must not be reloaded away."""
    try:
        return bool(page.evaluate(_OPEN_CHALLENGE))
    except PlaywrightError:
        return False


def _wait_for_challenge_image(page: Page) -> None:
    """Give the task picture a chance to load before taking the screenshot."""
    try:
        page.wait_for_function(
            "() => Array.from(document.images).some(image => image.naturalWidth > 0)",
            timeout=IMAGE_SETTLE_MS,
        )
    except PlaywrightError:
        pass


def prepare_challenge(
    page: Page,
    pending: ChallengePending,
    root: Path,
    media_filter: Callable[[Route], None],
) -> CaptchaRequest:
    """Reload the check once with media enabled and publish the request."""
    identifier = uuid.uuid4().hex[:12]
    pending_dir = root / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    screenshot = pending_dir / f"{identifier}.png"
    try:
        page.unroute("**/*", media_filter)
        # CAPTCHA-KEEP-OPEN: a second stage opens inside the page (a puzzle over
        # the refusal notice). Reloading would close it and start the check over.
        if not _challenge_on_screen(page):
            page.reload(wait_until="domcontentloaded")
        _wait_for_challenge_image(page)
        page.screenshot(path=str(screenshot))
        start = _challenge_centre(page)
    finally:
        page.route("**/*", media_filter)

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
    (pending_dir / f"{identifier}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return request


class BrokerSolver:
    """Read the answer an operator (or an agent) writes next to the request."""

    def __init__(self, root: Path, wait: float = CAPTCHA_WAIT_S) -> None:
        self._root = root
        self._wait = wait

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
        return (GiveUp(),)


def _one_action(item: object) -> Action | None:
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
            return Pause(float(cast(float, entry["seconds"])))
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _read_answer(path: Path, _request: CaptchaRequest) -> tuple[Action, ...]:
    # CAPTCHA-SEQUENCE: one check can need several steps, for example a block
    # notice whose button opens the slider. Both a single action and a list are
    # accepted, so a short answer stays short.
    try:
        payload = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return (GiveUp(),)
    listed = payload.get("actions")
    items: list[object] = (
        list(cast(list[object], listed)) if isinstance(listed, list) else [payload]
    )
    actions: list[Action] = []
    for item in items:
        action = _one_action(item)
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
        if isinstance(action, GiveUp):
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
