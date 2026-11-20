"""Read one supported public card and submit its snapshot to the catalog."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page

from app.catalog.contracts import Deal, Snapshot, SnapshotResult
from app.catalog.locks import run_session
from app.catalog.publications import accept_snapshot, publication_deal
from app.catalog.references import source_enabled
from app.collection.browser import (
    CollectionError,
    browser_page,
    check_stop,
    read_page,
    warm_profile,
    without_media,
)
from app.collection.captcha import Solver
from app.collection.collectors import avito, cian, domclick
from app.collection.corpus import Corpus
from app.collection.domclick_normalization import DailyListingQuote
from app.collection.observations import Observe


def collect_card(
    url: str,
    stopped: Callable[[], bool],
    *,
    solver: Solver | None = None,
    captcha_root: Path | None = None,
) -> SnapshotResult:
    source, key = _card_identity(url)

    check_stop(stopped)
    with run_session():
        if not source_enabled(source):
            raise CollectionError(f"{source}_is_disabled")
        check_stop(stopped)
        removed_deal = publication_deal(source, key) if source == "avito" else None
        with browser_page() as page:
            warm_profile(page, url, stopped)
            page.route("**/*", without_media)
            snapshot = read_card(
                page,
                url,
                source,
                stopped,
                removed_deal,
                solver=solver,
                captcha_root=captcha_root,
            )

        check_stop(stopped)
        receipt = (
            snapshot
            if isinstance(snapshot, SnapshotResult)
            else accept_snapshot(snapshot)
        )
        if receipt.outcome not in ("created", "updated", "unchanged", "not_found"):
            raise CollectionError(receipt.reason or receipt.outcome)
        return receipt


def _card_identity(url: str) -> tuple[str, str]:
    for source, adapter in (("avito", avito), ("cian", cian), ("domclick", domclick)):
        if key := adapter.card_key(url):
            return source, key
    raise CollectionError("unsupported_card_url")


def collect_corpus_card(
    url: str, directory: Path, stopped: Callable[[], bool]
) -> dict[str, object]:
    source, key = _card_identity(url)
    corpus = Corpus(directory)
    check_stop(stopped)
    if corpus.contains(source, key):
        outcome = "already_saved"
    else:
        with browser_page() as page:
            page.route("**/*", without_media)
            outcome = "saved"
            try:
                card = read_card(page, url, source, stopped, None, observe=corpus.save)
            except CollectionError as error:
                if not corpus.saved:
                    raise
                outcome = str(error)
            else:
                if not corpus.saved:
                    outcome = (
                        card.outcome
                        if isinstance(card, SnapshotResult)
                        else card["state"]
                    )
    return {
        "source": source,
        "external_id": key,
        "outcome": outcome,
        "saved": corpus.saved,
    }


def read_card(
    page: Page,
    url: str,
    source: str,
    stopped: Callable[[], bool],
    removed_deal: Deal | None,
    *,
    quote: DailyListingQuote | None = None,
    solver: Solver | None = None,
    captcha_root: Path | None = None,
    observe: Observe | None = None,
) -> Snapshot | SnapshotResult:
    same_card = {
        "avito": avito.same_card,
        "cian": cian.same_card,
        "domclick": domclick.same_card,
    }[source]
    return read_page(
        page,
        url,
        stopped,
        lambda observed_at, deadline: capture_card(
            page,
            url,
            source,
            observed_at,
            deadline,
            removed_deal,
            quote=quote,
            observe=observe,
        ),
        accepts_redirect=lambda loaded: same_card(url, loaded),
        solver=solver,
        captcha_root=captcha_root,
    )


def capture_card(
    page: Page,
    url: str,
    source: str,
    observed_at: datetime,
    deadline: float,
    removed_deal: Deal | None,
    *,
    quote: DailyListingQuote | None = None,
    observe: Observe | None = None,
) -> Snapshot | SnapshotResult:
    if source == "avito":
        return avito.capture_card(
            page,
            url,
            observed_at,
            deadline,
            removed_deal=removed_deal,
            observe=observe,
        )
    if source == "domclick":
        return domclick.read_card(
            page, url, observed_at, deadline=deadline, quote=quote, observe=observe
        )
    return cian.capture_card(page, url, observed_at, deadline, observe=observe)
