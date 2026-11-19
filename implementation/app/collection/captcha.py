"""Ask for and apply one answer to a detected access challenge."""

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from playwright.sync_api import Page, Route

CaptchaKind = Literal["click", "drag", "unknown"]

CAPTCHA_WAIT_S = 120.0
POLL_INTERVAL_S = 0.3
DRAG_STEPS = 12
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


@dataclass(frozen=True)
class GiveUp:
    """The check stays unresolved; the page attempt keeps its own reason."""


Action = Click | Drag | GiveUp


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
        page.reload(wait_until="domcontentloaded")
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


def _read_answer(path: Path, request: CaptchaRequest) -> tuple[Action, ...]:
    try:
        payload = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return (GiveUp(),)
    action = payload.get("action")
    try:
        if action == "click":
            return (
                Click(
                    float(cast(float, payload["x"])), float(cast(float, payload["y"]))
                ),
            )
        if action == "drag":
            return (
                Drag(
                    float(cast(float, payload["dx"])), float(cast(float, payload["dy"]))
                ),
            )
    except (KeyError, TypeError, ValueError):
        return (GiveUp(),)
    return (GiveUp(),)


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
        elif isinstance(action, Drag):
            x, y = request.start
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
