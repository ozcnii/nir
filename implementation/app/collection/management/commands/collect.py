"""Collect a card or a verified regional search in the foreground."""

import json
import signal
from argparse import ArgumentParser
from typing import get_args

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from playwright.sync_api import Error as PlaywrightError

from app.catalog.contracts import Deal
from app.catalog.locks import CollectionBusy
from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    HOUSE_TYPE_ID,
    ROOM_TYPE_ID,
    SOURCE_CODES,
)
from app.collection.one_card import (
    CollectionError,
    CollectionStopped,
    collect_card,
)
from app.collection.search import collect_search

_PROPERTY_TYPES = {
    "apartment": APARTMENT_TYPE_ID,
    "room": ROOM_TYPE_ID,
    "house": HOUSE_TYPE_ID,
}


class Command(BaseCommand):
    help = "Collect one card or traverse a verified regional search."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("url", nargs="?")
        parser.add_argument("--source", choices=SOURCE_CODES)
        parser.add_argument("--region", type=int)
        parser.add_argument("--property-type", choices=_PROPERTY_TYPES)
        parser.add_argument("--deal", choices=get_args(Deal))

    def handle(
        self,
        *,
        url: str | None,
        source: str | None,
        region: int | None,
        property_type: str | None,
        deal: Deal | None,
        **options: object,
    ) -> None:
        stop_requested = False

        def request_stop(_signum: int, _frame: object) -> None:
            nonlocal stop_requested
            stop_requested = True

        signals = (signal.SIGINT, signal.SIGTERM)
        handlers = {signum: signal.signal(signum, request_stop) for signum in signals}
        try:
            if url is not None:
                if any(
                    value is not None for value in (source, region, property_type, deal)
                ):
                    raise CommandError("choose_card_or_collection_scope")
                receipt = collect_card(url, lambda: stop_requested)
                result: dict[str, object] = {
                    "outcome": receipt.outcome,
                    "ad_id": receipt.ad_id,
                    "property_id": receipt.property_id,
                    "price_changed": receipt.price_changed,
                }
            elif (
                source is not None
                and region is not None
                and property_type is not None
                and deal is not None
            ):
                result = collect_search(
                    source,
                    region,
                    _PROPERTY_TYPES[property_type],
                    deal,
                    lambda: stop_requested,
                )
            else:
                raise CommandError("collection_scope_required")
        except CollectionStopped:
            self.stdout.write("Collection stopped.")
            return
        except (CollectionError, CollectionBusy) as error:
            raise CommandError(str(error)) from error
        except PlaywrightError as error:
            raise CommandError("browser_failed") from error
        except DatabaseError as error:
            raise CommandError("database_error") from error
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)

        self.stdout.write(json.dumps(result, ensure_ascii=False))
