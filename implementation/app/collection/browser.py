"""Open a headless Chromium page for the collector."""

import logging
import os
import random
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from random import uniform
from typing import Literal
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, ProxySettings, Response, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.collection.captcha import (
    Challenge,
    ChallengePending,
    Done,
    NoAnswer,
    Solver,
    apply_answer,
    loaded_images,
    prepare_challenge,
)
from app.collection.normalization import RejectedCard, as_object

LOGGER = logging.getLogger(__name__)

# ACCESS-CHECK-NAVIGATION: a challenge replaces the requested document, so it
# must be recognised before the status or the address is treated as a refusal.
_CHECK_STATUSES = frozenset({401, 403, 429, 439})
# Маркеры, за которыми стоит только сообщение о блокировке, без задания.
_NOTICE_MARKERS = frozenset({"block_notice", "confirm_notice"})
CHECK_SETTLE_MS = 5_000.0
# Часть проверок проходит сама: страница закрывает их после короткого ожидания.
# Прежде чем звать агента, даём задаче исчезнуть самостоятельно.
CHALLENGE_SELF_RESOLVE_S = 15.0
# После нажатия кнопки проверка думает секунды: ждём, а не жмём повторно.
CONTINUE_SETTLE_S = 25.0
# Сколько ждать появления кнопки продолжения после обнаружения проверки.
CONTINUE_WAIT_S = 8.0
_CHALLENGE_DOCUMENT = r"""() => {
    if (document.readyState !== "complete") return null;
    const text = `${document.title || ""}\n${(document.body?.innerText || "").slice(0, 4000)}`;
    if (document.querySelector('#js-challenge-loader')) return 'js-challenge-loader';
    if (document.querySelector('iframe[src*="geetest"], script[src*="geetest"], [class*="geetest_"]')) return 'geetest';
    if (document.querySelector('script[src*="servicepipe"]') && /\/xpvnsulc\//.test(location.pathname)) return '/xpvnsulc/';
    if (/\/xpvnsulc\//.test(location.pathname)) return '/xpvnsulc/';
    if (/нажмите для подтверждения|подтвердите, что вы не робот/i.test(text)) return 'confirm_notice';
    if (/доступ ограничен|проблемы с ip|подозрительн|проверка безопасности|убедиться[^.]{0,40}ботом/i.test(text)) return 'block_notice';
    return null;
}"""


def _challenge_source(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    for source, suffix in (
        ("avito", "avito.ru"),
        ("cian", "cian.ru"),
        ("domclick", "domclick.ru"),
    ):
        if host == suffix or host.endswith("." + suffix):
            return source
    return "unknown"


# Кнопка продолжения на странице проверки. Её текст стабилен, поэтому нажимаем
# селектором, а не просим модель искать координаты.
_CONTINUE_CONTROL = {
    "avito": (
        "button:has-text('Пройти проверку')",
        "button:has-text('Продолжить')",
        "text=Нажмите для подтверждения",
    ),
}


def _human_reload(page: Page) -> None:
    """Обновляет страницу как человек: курсор в сторону, F5, пауза.

    Программный reload заметен площадке, а обычное обновление страницы
    возвращает её к нормальному виду, как это делает оператор.
    """
    try:
        page.mouse.move(
            random.uniform(180.0, 900.0),
            random.uniform(120.0, 620.0),
            steps=random.randint(10, 20),
        )
        time.sleep(random.uniform(0.4, 1.0))
        page.keyboard.press("F5")
        # После обновления человек не жмёт следующее сразу: пауза длиннее,
        # чем загрузка страницы, иначе площадка видит серию запросов.
        page.wait_for_timeout(int(random.uniform(8000.0, 15000.0)))
    except PlaywrightError:
        pass


def _click_continue(page: Page, source: str) -> bool:
    """Нажимает кнопку продолжения, если для источника она известна."""
    selectors = _CONTINUE_CONTROL.get(source)
    if not selectors:
        return False
    try:
        control = None
        # Кнопка появляется не мгновенно: страница сначала дорисовывает модалку.
        deadline = time.monotonic() + CONTINUE_WAIT_S
        while control is None and time.monotonic() < deadline:
            for selector in selectors:
                candidate = page.locator(selector).first
                if candidate.count() > 0 and candidate.is_visible():
                    control = candidate
                    break
            if control is None:
                page.wait_for_timeout(500)
        if control is None:
            return False
        box = control.bounding_box()
        if box is None:
            return False
        # Человеческий клик: пауза, случайная точка внутри кнопки, путь из
        # многих шагов и небольшая задержка перед отпусканием.
        time.sleep(random.uniform(0.4, 1.2))
        x = box["x"] + random.uniform(0.25, 0.75) * box["width"]
        y = box["y"] + random.uniform(0.25, 0.75) * box["height"]
        page.mouse.move(
            box["x"] + box["width"] / 2 + random.uniform(-120.0, 120.0),
            box["y"] + box["height"] / 2 + random.uniform(-80.0, 80.0),
        )
        page.mouse.move(x, y, steps=random.randint(15, 25))
        time.sleep(random.uniform(0.1, 0.3))
        page.mouse.down()
        time.sleep(random.uniform(0.05, 0.15))
        page.mouse.up()
        return True
    except PlaywrightError:
        return False


# Задача может прийти фреймом или виджетом в самом документе: у hCaptcha
# пазл GeeTest рисуется в странице, а её собственные фреймы остаются пустыми.
_FRAME_TASK = (
    '[class*="geetest_slider"], [class*="geetest_holder"], [class*="geetest_panel"],'
    ' iframe[src*="frame=challenge"], iframe[src*="puzzle"], iframe[src*="geetest"]'
)
# Сколько ждать, пока нажатая кнопка откроет задачу во фрейме.
FRAME_TASK_WAIT_S = 6.0
# Кнопка проверки иногда не открывает окно с первого раза: hCaptcha
# успевает не инициализироваться. Повторяем, пока задача не появится.
CLICK_ATTEMPTS = 4
# Сколько ждать решения после того, как ответ применён на странице.
ANSWER_SETTLE_S = 12.0


def _frame_task_visible(page: Page, seconds: float) -> bool:
    """Открылась ли задача во фрейме: её нельзя ждать вместе с «проверка ушла»."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            visible = bool(
                page.evaluate(
                    """(selector) => Array.from(document.querySelectorAll(selector))
                        .some(node => {
                            const rect = node.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0
                                && rect.bottom > 0 && rect.top < innerHeight;
                        })""",
                    _FRAME_TASK,
                )
            )
        except PlaywrightError:
            visible = False
        if visible:
            return True
        time.sleep(0.4)
    return False


def _challenge_gone(page: Page, seconds: float) -> bool:
    """Ждёт, пока проверка закроется сама; True — закрылась без вмешательства."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _document_challenge(page, timeout_ms=500.0) is None:
            return True
        time.sleep(0.5)
    return False


def _document_challenge(
    page: Page, timeout_ms: float = CHECK_SETTLE_MS
) -> ChallengePending | None:
    # The refusal document replaces the page after the navigation event, so the
    # check page is often still loading when the status is already known.
    try:
        page.wait_for_function(
            "() => document.readyState === 'complete'", timeout=timeout_ms
        )
    except PlaywrightError:
        return None
    try:
        marker = page.evaluate(_CHALLENGE_DOCUMENT)
    except PlaywrightError:
        return None
    if not isinstance(marker, str) or not marker:
        return None
    return ChallengePending(
        Challenge(_challenge_source(page.url), "drag", marker, replaces_document=True)
    )


MAX_ATTEMPTS = 2
CARD_TIMEOUT_MS = 30_000
# Avito resolves its own firewall check in ~6 s on a healthy address, so the
# observation window stays just under the attempt limit instead of failing early.
CHALLENGE_BUDGET_S = 25.0
RETRY_PAUSE_S = 2.0
WARM_SETTLE_MS = 3_000
STOP_POLL_S = 0.2
# One source can sample a few pages and cards at a human pace instead of
# draining the whole listing in a burst. Limits and pauses are empty by default
# so checks walk the complete listing; a live run sets its own pace.
# Сколько раз за одну попытку страницы разрешено проходить проверку. У Авито
# её ступени идут подряд: кнопка, пазл, после перезагрузки снова кнопка.
CAPTCHA_ROUNDS = int(os.environ.get("COLLECTION_CAPTCHA_ROUNDS", "4"))
COLLECTION_MAX_PAGES = ""
COLLECTION_MAX_CARDS = ""
COLLECTION_PAGE_PAUSE = ""
COLLECTION_CARD_PAUSE = ""
_OBSERVE_STATUSES = frozenset({401, 403, 439})


def default_captcha_root() -> Path:
    """Where a run publishes challenge requests for an operator answer."""
    return Path(tempfile.gettempdir()) / "nir-captcha"


def default_profile_root() -> Path:
    """Where named browser profiles keep their cookies between runs."""
    return Path.home() / ".cache" / "nir-collection"


TRANSPORT_ERRORS = (
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_CONNECTION_ABORTED",
    "net::ERR_TIMED_OUT",
    "net::ERR_NETWORK_CHANGED",
)


class CollectionError(RuntimeError):
    pass


Recovery = Literal["proceed", "observe", "retry", "stop"]


def document_recovery(status: int, *, navigation: bool) -> Recovery:
    """Decide what one page attempt does with an observed HTTP status.

    401/403/439 are intermediate: the site resolves its own check, so the
    document is observed for a short window instead of failing at once.
    """
    if 200 <= status < 300:
        return "proceed"
    if status == 429 or 500 <= status < 600:
        return "retry"
    if navigation and status in _OBSERVE_STATUSES:
        return "observe"
    return "stop"


def reason_recovery(reason: str) -> Recovery:
    """Classify a rejection raised while reading a response body."""
    if not reason.startswith("http_"):
        return "stop"
    try:
        status = int(reason.removeprefix("http_"))
    except ValueError:
        return "stop"
    return document_recovery(status, navigation=False)


class CollectionStopped(Exception):
    pass


class BrowserTransportError(PlaywrightError):
    pass


class DocumentResponseError(RejectedCard):
    def __init__(self, response: Response) -> None:
        super().__init__(f"http_{response.status}")
        self.url = response.url
        self.status = response.status


def check_stop(stopped: Callable[[], bool]) -> None:
    if stopped():
        raise CollectionStopped


@dataclass(frozen=True)
class CollectionPace:
    """How many pages and cards one source samples, and how fast."""

    max_pages: int | None
    max_cards: int | None
    page_pause: tuple[float, float] | None
    card_pause: tuple[float, float] | None


def collection_pace() -> CollectionPace:
    """Read this run's sampling limits and pauses from the environment."""
    return CollectionPace(
        max_pages=_read_limit("COLLECTION_MAX_PAGES", COLLECTION_MAX_PAGES),
        max_cards=_read_limit("COLLECTION_MAX_CARDS", COLLECTION_MAX_CARDS),
        page_pause=_read_pause("COLLECTION_PAGE_PAUSE", COLLECTION_PAGE_PAUSE),
        card_pause=_read_pause("COLLECTION_CARD_PAUSE", COLLECTION_CARD_PAUSE),
    )


def _read_limit(variable: str, default: str) -> int | None:
    text = os.environ.get(variable, default).strip()
    if not text or text == "0":
        return None
    try:
        value = int(text)
    except ValueError as error:
        raise CollectionError(f"invalid_{variable.lower()}") from error
    if value < 0:
        raise CollectionError(f"invalid_{variable.lower()}")
    return value


def _read_pause(variable: str, default: str) -> tuple[float, float] | None:
    text = os.environ.get(variable, default).strip()
    if not text:
        return None
    low, _, high = text.partition("-")
    try:
        bounds = float(low), float(high or low)
    except ValueError as error:
        raise CollectionError(f"invalid_{variable.lower()}") from error
    if bounds[0] < 0 or bounds[1] < bounds[0]:
        raise CollectionError(f"invalid_{variable.lower()}")
    return bounds


def wait_between(
    pause: tuple[float, float] | None, stopped: Callable[[], bool]
) -> None:
    """Wait a human-like moment without ignoring a stop request."""
    if pause is None:
        return
    deadline = time.monotonic() + uniform(*pause)
    while (remaining := deadline - time.monotonic()) > 0:
        check_stop(stopped)
        time.sleep(min(remaining, STOP_POLL_S))
    check_stop(stopped)


def warm_profile(page: Page, url: str, stopped: Callable[[], bool]) -> None:
    """Visit the site root once so a named profile starts with its own cookies."""
    settings = browser_settings()
    parts = urlsplit(url)
    if settings.profile is None or not parts.scheme or not parts.netloc:
        return
    check_stop(stopped)
    try:
        page.goto(
            f"{parts.scheme}://{parts.netloc}/",
            wait_until="domcontentloaded",
            timeout=CARD_TIMEOUT_MS,
        )
        page.wait_for_timeout(WARM_SETTLE_MS)
    except PlaywrightError:
        return
    check_stop(stopped)


def without_media(route: Route) -> None:
    if route.request.resource_type in ("image", "media"):
        route.abort()
    else:
        route.fallback()


@dataclass(frozen=True)
class BrowserSettings:
    """How this run reaches the sources; read from the environment per run."""

    profile: str | None
    profile_root: Path
    channel: str
    headless: bool
    proxy: str | None


def browser_settings() -> BrowserSettings:
    """A named profile keeps the site cookies between runs."""
    profile = os.environ.get("COLLECTION_PROFILE", "").strip() or None
    root = os.environ.get("COLLECTION_PROFILE_ROOT", "").strip()
    headless = os.environ.get("COLLECTION_BROWSER_HEADLESS", "1").strip()
    return BrowserSettings(
        profile=profile,
        profile_root=Path(root) if root else default_profile_root(),
        channel=os.environ.get("COLLECTION_BROWSER_CHANNEL", "").strip() or "chromium",
        headless=headless not in ("0", "false", "False"),
        proxy=os.environ.get("COLLECTION_PROXY", "").strip() or None,
    )


@contextmanager
def browser_page() -> Iterator[Page]:
    """Open the page this run reads with: a named profile or an ephemeral one."""
    settings = browser_settings()
    proxy: ProxySettings | None = (
        {"server": settings.proxy} if settings.proxy is not None else None
    )
    with sync_playwright() as playwright:
        if settings.profile is None:
            with (
                playwright.chromium.launch(
                    channel=settings.channel,
                    headless=settings.headless,
                    chromium_sandbox=True,
                    handle_sigint=False,
                    handle_sigterm=False,
                    proxy=proxy,
                ) as browser,
                browser.new_context(accept_downloads=False) as context,
            ):
                yield context.new_page()
            return

        directory = settings.profile_root / settings.profile
        try:
            directory.mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(directory),
                channel=settings.channel,
                headless=settings.headless,
                accept_downloads=False,
                handle_sigint=False,
                handle_sigterm=False,
                proxy=proxy,
                viewport={"width": 1440, "height": 900},
                locale="ru-RU",
                timezone_id="Europe/Samara",
                # Playwright по умолчанию оставляет --enable-automation, из-за
                # которого страница видит navigator.webdriver и считает браузер
                # роботом. Для обычного чтения публичных страниц это лишний флаг.
                ignore_default_args=["--enable-automation", "--no-sandbox"],
                args=["--disable-blink-features=AutomationControlled"],
            )
        except OSError as error:
            raise CollectionError("profile_unavailable") from error
        try:
            yield context.pages[0] if context.pages else context.new_page()
        finally:
            context.close()


@contextmanager
def browser_post_status(page: Page, url: str) -> Iterator[Callable[[], int | None]]:
    """Observe HTTP errors that Chromium hides from cross-origin fetch."""
    session = page.context.new_cdp_session(page)
    requests: set[str] = set()
    failure: int | None = None

    def request_started(event: dict[str, object]) -> None:
        request = as_object(event.get("request"))
        identifier = event.get("requestId")
        if (
            isinstance(identifier, str)
            and request.get("url") == url
            and request.get("method") in ("POST", "OPTIONS")
        ):
            requests.add(identifier)

    def response_received(event: dict[str, object]) -> None:
        nonlocal failure
        identifier, status = event.get("requestId"), event.get("statusCode")
        if (
            isinstance(identifier, str)
            and identifier in requests
            and type(status) is int
        ):
            if 400 <= status < 600:
                failure = status

    try:
        session.on("Network.requestWillBeSent", request_started)
        session.on("Network.responseReceivedExtraInfo", response_received)
        session.send("Network.enable")
        yield lambda: failure
    finally:
        if not page.is_closed():
            session.detach()


def read_page[Result](
    page: Page,
    url: str | None,
    stopped: Callable[[], bool],
    capture: Callable[[datetime, float], Result],
    *,
    accepts_redirect: Callable[[str], bool],
    next_control: str | None = None,
    advance: Callable[[float], Result] | None = None,
    challenge_budget: float = CHALLENGE_BUDGET_S,
    retry_pause: float = RETRY_PAUSE_S,
    solver: Solver | None = None,
    captcha_root: Path | None = None,
) -> Result:
    responses: list[Response] = []
    if next_control is not None and advance is None:

        def document_response(response: Response) -> None:
            if (
                response.request.is_navigation_request()
                and response.frame == page.main_frame
            ):
                responses.append(response)

        page.on("response", document_response)
        try:
            return _read_page(
                page,
                url,
                stopped,
                capture,
                accepts_redirect,
                next_control,
                advance,
                responses,
                challenge_budget,
                retry_pause,
                solver,
                captcha_root,
            )
        finally:
            page.remove_listener("response", document_response)
    return _read_page(
        page,
        url,
        stopped,
        capture,
        accepts_redirect,
        next_control,
        advance,
        responses,
        challenge_budget,
        retry_pause,
        solver,
        captcha_root,
    )


def _read_page[Result](
    page: Page,
    url: str | None,
    stopped: Callable[[], bool],
    capture: Callable[[datetime, float], Result],
    accepts_redirect: Callable[[str], bool],
    next_control: str | None,
    advance: Callable[[float], Result] | None,
    responses: list[Response],
    challenge_budget: float,
    retry_pause: float,
    solver: Solver | None,
    captcha_root: Path | None,
) -> Result:
    direct_url = url if next_control is None else None
    clicked = False
    attempts = 0
    observed_status: int | None = None
    broker_rounds = 0
    media_released = False
    resume_read = False

    image_baseline: frozenset[str] | None = None
    continue_clicked = False

    def resolve(pending: ChallengePending) -> bool:
        """Pass one challenge: wait it out, click a known control, or ask the broker."""
        nonlocal broker_rounds, media_released, image_baseline, continue_clicked
        if not media_released:
            # A check is interactive: its control and the next stage need their
            # pictures even before an answer arrives.
            page.unroute("**/*", without_media)
            media_released = True
        clicked_any = False
        if not continue_clicked:
            before_click = loaded_images(page)
            for attempt in range(CLICK_ATTEMPTS):
                if not _click_continue(page, pending.challenge.source):
                    # Кнопки нет: блокировка иногда снимается обычным
                    # обновлением страницы, как это сделал бы человек.
                    if attempt + 1 < CLICK_ATTEMPTS:
                        _human_reload(page)
                    continue
                # CAPTCHA-NEXT-STAGE: the continue button opens the next stage
                # over the same document (a puzzle over the refusal notice). The
                # check is still there by design, so the broker answers that
                # stage instead of the click counting as a failed attempt.
                continue_clicked = True
                clicked_any = True
                image_baseline = before_click
                # Задача в фрейме открывается сразу после клика и живёт
                # недолго: ждать её закрытия вместе с отказом нельзя.
                if _frame_task_visible(page, FRAME_TASK_WAIT_S):
                    break
                if _challenge_gone(page, CONTINUE_SETTLE_S):
                    return True
                # Кнопка не открыла задачу: обновляем страницу как человек.
                _human_reload(page)
        if _challenge_gone(page, CHALLENGE_SELF_RESOLVE_S):
            return True
        if not clicked_any and not continue_clicked:
            # Кнопки так и не было: модели на такой странице нечего измерять.
            return False
        if broker_rounds >= CAPTCHA_ROUNDS or solver is None:
            return False
        broker_rounds += 1
        root = captcha_root if captcha_root is not None else default_captcha_root()
        request = prepare_challenge(
            page,
            pending,
            root,
            reload=(
                broker_rounds == 1
                and pending.challenge.replaces_document
                # CAPTCHA-KEEP-WIDGET: перезагрузка закрывает задачу, которую
                # только что открыла нажатая кнопка.
                and not continue_clicked
                and not bool(getattr(solver, "handover", False))
            ),
            wait_for_new=image_baseline,
        )
        actions = solver(request, stopped)
        check_stop(stopped)
        if any(isinstance(action, Done) for action in actions):
            return True
        if any(isinstance(action, NoAnswer) for action in actions):
            return False
        # The next stage may open only after this answer is applied; remember
        # what the document showed so the next screenshot waits for something new.
        image_baseline = loaded_images(page)
        applied = apply_answer(page, request, actions)
        # CAPTCHA-VERIFY: ответ мог быть непринятым, поэтому недостаточно
        # самого действия — ждём, что проверка действительно закрылась.
        if applied and _challenge_gone(page, ANSWER_SETTLE_S):
            return True
        if not applied:
            return False
        # Задача открывается заново тем же кликом, но уже с другой картинкой.
        continue_clicked = False
        if not _click_continue(page, pending.challenge.source):
            return False
        if not _frame_task_visible(page, FRAME_TASK_WAIT_S):
            return False
        return resolve(pending)

    try:
        while True:
            navigation = not resume_read
            resume_read = False
            if navigation:
                if attempts >= MAX_ATTEMPTS:
                    reason = (
                        f"http_{observed_status}"
                        if observed_status is not None
                        else "browser_read_failed"
                    )
                    raise CollectionError(reason)
                attempts += 1
                observed_status = None
            check_stop(stopped)
            observed_at = datetime.now(UTC)
            deadline = time.monotonic() + CARD_TIMEOUT_MS / 1000
            try:
                if navigation and direct_url is not None:
                    response = page.goto(
                        direct_url,
                        wait_until="domcontentloaded",
                        timeout=CARD_TIMEOUT_MS,
                    )
                    check_stop(stopped)
                    if response is None:
                        raise CollectionError("no_http_response")
                    recovery = document_recovery(response.status, navigation=True)
                    if response.status in _CHECK_STATUSES:
                        found = _document_challenge(page)
                        if found is not None:
                            resolved = resolve(found)
                            if not resolved and solver is not None:
                                raise CollectionError("source_captcha") from None
                            if resolved:
                                attempts = max(attempts - 1, 0)
                                resume_read = (
                                    _document_challenge(page, timeout_ms=800.0) is None
                                )
                                continue
                    if recovery == "stop":
                        raise CollectionError(f"http_{response.status}")
                    if recovery == "retry":
                        if attempts < MAX_ATTEMPTS:
                            if response.status == 429:
                                time.sleep(retry_pause)
                            continue
                        raise CollectionError(f"http_{response.status}")
                    if recovery == "observe":
                        observed_status = response.status
                        deadline = min(deadline, time.monotonic() + challenge_budget)
                elif (
                    navigation
                    and advance is None
                    and not clicked
                    and next_control is not None
                ):
                    control = page.locator(next_control)
                    control.click(trial=True, timeout=CARD_TIMEOUT_MS)
                    check_stop(stopped)
                    # After dispatch the same control can refer to another page.
                    clicked = True
                    control.click(timeout=max(1, (deadline - time.monotonic()) * 1000))
                    if responses and not responses[-1].ok:
                        raise DocumentResponseError(responses[-1])
                check_stop(stopped)
                result = (
                    advance(deadline)
                    if navigation and advance is not None and direct_url is None
                    else capture(observed_at, deadline)
                )
                check_stop(stopped)
                if time.monotonic() >= deadline:
                    raise PlaywrightTimeout("Page attempt deadline elapsed")
                return result
            except ChallengePending as pending:
                check_stop(stopped)
                LOGGER.warning(
                    "challenge: source=%s marker=%s status=%s",
                    pending.challenge.source,
                    pending.challenge.marker,
                    observed_status,
                )
                if not resolve(pending):
                    raise CollectionError("source_captcha") from None
                attempts = max(attempts - 1, 0)
                resume_read = _document_challenge(page, timeout_ms=800.0) is None
                continue
            except RejectedCard as error:
                check_stop(stopped)
                if isinstance(error, DocumentResponseError):
                    if (
                        attempts < MAX_ATTEMPTS
                        and 500 <= error.status < 600
                        and accepts_redirect(error.url)
                    ):
                        direct_url = error.url
                        continue
                    raise CollectionError(str(error)) from error
                if attempts < MAX_ATTEMPTS and reason_recovery(str(error)) == "retry":
                    if str(error) == "http_429":
                        time.sleep(retry_pause)
                    continue
                raise CollectionError(str(error)) from error
            except PlaywrightError as error:
                check_stop(stopped)
                if page.is_closed():
                    raise
                if "Execution context was destroyed" in str(
                    error
                ) and not accepts_redirect(page.url):
                    found = _document_challenge(page)
                    if found is not None:
                        resolved = resolve(found)
                        if not resolved and solver is not None:
                            raise CollectionError("source_captcha") from None
                        if resolved:
                            attempts = max(attempts - 1, 0)
                            resume_read = (
                                _document_challenge(page, timeout_ms=800.0) is None
                            )
                            continue
                    raise CollectionError("unexpected_redirect") from error
                if observed_status is not None:
                    raise CollectionError(f"http_{observed_status}") from error
                if attempts >= MAX_ATTEMPTS or not (
                    isinstance(error, (BrowserTransportError, PlaywrightTimeout))
                    or any(code in str(error) for code in TRANSPORT_ERRORS)
                ):
                    raise CollectionError("browser_read_failed") from error
    finally:
        if media_released and not page.is_closed():
            page.route("**/*", without_media)
