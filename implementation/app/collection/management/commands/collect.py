"""Collect a publication in the foreground."""

import json
import signal
from argparse import ArgumentParser

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from playwright.sync_api import Error as PlaywrightError

from app.catalog.locks import CollectionBusy
from app.collection.one_card import (
    CollectionError,
    CollectionStopped,
    collect_card,
)


class Command(BaseCommand):
    help = "Collect one supported public listing."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("url")

    def handle(self, *, url: str, **options: object) -> None:
        stop_requested = False

        def request_stop(_signum: int, _frame: object) -> None:
            nonlocal stop_requested
            stop_requested = True

        signals = (signal.SIGINT, signal.SIGTERM)
        handlers = {signum: signal.signal(signum, request_stop) for signum in signals}
        try:
            receipt = collect_card(url, lambda: stop_requested)
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

        self.stdout.write(
            json.dumps(
                {
                    "outcome": receipt.outcome,
                    "ad_id": receipt.ad_id,
                    "property_id": receipt.property_id,
                    "price_changed": receipt.price_changed,
                }
            )
        )
