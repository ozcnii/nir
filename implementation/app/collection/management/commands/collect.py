"""Collect a card or a verified regional search in the foreground."""

import json
import os
import shutil
import signal
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import get_args

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from playwright.sync_api import Error as PlaywrightError

from app.catalog.contracts import Deal, Market
from app.catalog.locks import CollectionBusy
from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    HOUSE_TYPE_ID,
    ROOM_TYPE_ID,
    SOURCE_CODES,
)
from app.collection.browser import CollectionError, CollectionStopped
from app.collection.captcha import CAPTCHA_WAIT_S, BrokerSolver
from app.collection.one_card import collect_card, collect_corpus_card
from app.collection.search import collect_corpus, collect_search

# Тот же каталог, что и у приложения: обвязка лежит рядом с проектом.
_AGENT_SCRIPT = Path(__file__).resolve().parents[4] / "tools" / "captcha_agent.py"


def _agent_model() -> str:
    """Модель задаётся только окружением: скрытого значения по умолчанию нет."""
    model = os.environ.get("CAPTCHA_MODEL", "").strip()
    if not model:
        raise CommandError("captcha_agent_model_not_set: задайте CAPTCHA_MODEL")
    return model


def _start_agent(root: Path) -> subprocess.Popen[bytes]:
    """Поднимает решателя рядом со сбором и проверяет готовность до запуска."""
    if not _AGENT_SCRIPT.is_file():
        raise CommandError("captcha_agent_script_missing")
    if shutil.which("pi") is None:
        raise CommandError("captcha_agent_requires_pi")
    model = _agent_model()
    ready = subprocess.run(
        ["pi", "auth", "check", "--model", model],
        capture_output=True,
        text=True,
        check=False,
    )
    if ready.returncode != 0:
        raise CommandError(f"captcha_agent_model_not_ready: {model}")
    (root / "pending").mkdir(parents=True, exist_ok=True)
    (root / "answers").mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([sys.executable, str(_AGENT_SCRIPT), "--root", str(root)])


def _stop_agent(agent: subprocess.Popen[bytes]) -> None:
    if agent.poll() is not None:
        return
    agent.terminate()
    try:
        agent.wait(timeout=10)
    except subprocess.TimeoutExpired:
        agent.kill()


_PROPERTY_TYPES = {
    "apartment": APARTMENT_TYPE_ID,
    "room": ROOM_TYPE_ID,
    "house": HOUSE_TYPE_ID,
}


class Command(BaseCommand):
    help = "Collect one card or traverse a verified regional search."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("url", nargs="?")
        parser.add_argument("--source", choices=SOURCE_CODES, action="append")
        parser.add_argument("--region", type=int)
        parser.add_argument("--property-type", choices=_PROPERTY_TYPES)
        parser.add_argument("--deal", choices=get_args(Deal))
        parser.add_argument("--segment", choices=get_args(Market))
        parser.add_argument(
            "--corpus-dir",
            type=Path,
            help="Save verified public card snapshots to a local directory.",
        )
        parser.add_argument("--captcha-broker", type=Path)
        parser.add_argument("--captcha-wait", type=float, default=CAPTCHA_WAIT_S)
        parser.add_argument(
            "--captcha-agent",
            action="store_true",
            help="поднять решателя pi на каталоге брокера и погасить его в конце",
        )

    def handle(
        self,
        *,
        url: str | None,
        source: str | list[str] | None,
        region: int | None,
        property_type: str | None,
        deal: Deal | None,
        segment: Market | None,
        captcha_broker: Path | None,
        captcha_wait: float,
        captcha_agent: bool,
        corpus_dir: Path | None = None,
        **options: object,
    ) -> None:
        sources = [source] if isinstance(source, str) else source
        stop_requested = False

        def request_stopped() -> bool:
            return stop_requested

        def request_stop(_signum: int, _frame: object) -> None:
            nonlocal stop_requested
            stop_requested = True

        solver = (
            BrokerSolver(captcha_broker, wait=captcha_wait, handover=not captcha_agent)
            if captcha_broker is not None
            else None
        )
        if not os.environ.get("COLLECTION_PROFILE", "").strip():
            # Одноразовый контекст площадка считает подозрительным и включает
            # более жёсткую проверку, которую скриптовый клик не проходит.
            raise CommandError(
                "collection_profile_required: задайте COLLECTION_PROFILE"
            )
        if captcha_agent and captcha_broker is None:
            raise CommandError("captcha_agent_requires_broker")
        agent = (
            _start_agent(captcha_broker) if captcha_agent and captcha_broker else None
        )
        signals = (signal.SIGINT, signal.SIGTERM)
        handlers = {signum: signal.signal(signum, request_stop) for signum in signals}
        try:
            if url is not None:
                if any(
                    value is not None
                    for value in (source, region, property_type, deal, segment)
                ):
                    raise CommandError("choose_card_or_collection_scope")
                if corpus_dir is not None:
                    result = collect_corpus_card(url, corpus_dir, request_stopped)
                else:
                    receipt = collect_card(
                        url,
                        request_stopped,
                        solver=solver,
                        captcha_root=captcha_broker,
                    )
                    result = {
                        "outcome": receipt.outcome,
                        "ad_id": receipt.ad_id,
                        "property_id": receipt.property_id,
                        "price_changed": receipt.price_changed,
                    }
            elif (
                sources is not None
                and region is not None
                and property_type is not None
                and deal is not None
            ):
                if segment is not None and (
                    property_type != "apartment" or deal != "sale"
                ):
                    raise CommandError("collection_segment_requires_apartment_sale")
                if corpus_dir is None:
                    result = collect_search(
                        sources,
                        region,
                        _PROPERTY_TYPES[property_type],
                        deal,
                        request_stopped,
                        segment=segment,
                        solver=solver,
                        captcha_root=captcha_broker,
                    )
                else:
                    result = collect_corpus(
                        corpus_dir,
                        sources,
                        region,
                        _PROPERTY_TYPES[property_type],
                        deal,
                        request_stopped,
                        segment=segment,
                        solver=solver,
                        captcha_root=captcha_broker,
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
        except OSError as error:
            if corpus_dir is None:
                raise
            raise CommandError(f"corpus_io_error: {error}") from error
        except DatabaseError as error:
            raise CommandError("database_error") from error
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            if agent is not None:
                _stop_agent(agent)

        self.stdout.write(json.dumps(result, ensure_ascii=False))
