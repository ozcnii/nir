import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import quote

from django.core.management import CommandError, call_command
from playwright.sync_api import Page, Route

from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    ROOM_TYPE_ID,
    SAMARA_REGION_ID,
)
from app.collection.browser import browser_page
from app.collection.collectors import avito, domclick
from app.collection.collectors.cian_search import route_for
from app.collection.collectors.domclick_search import routes_for
from app.collection.tests import avito_fixture, domclick_fixture
from app.collection.tests.cian_fixture import projection, projection_html
from app.collection.tests.test_cian_search import script


@contextmanager
def replay(responses: dict[str, str], visits: list[str]) -> Iterator[None]:
    def serve(request: Route) -> None:
        url = request.request.url
        if url not in responses:
            request.abort()
            return
        visits.append(url)
        request.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=responses[url]
        )

    @contextmanager
    def local_browser() -> Iterator[Page]:
        with browser_page() as page:
            page.context.route("**/*", serve)
            yield page

    with (
        patch("app.collection.search.browser_page", local_browser),
        patch("app.collection.one_card.browser_page", local_browser),
        patch("app.collection.search._catalog_worker", side_effect=AssertionError),
        patch("app.collection.one_card.run_session", side_effect=AssertionError),
        patch.dict(
            os.environ,
            {
                "COLLECTION_MAX_CARDS": "1",
                "COLLECTION_MAX_PAGES": "",
                "COLLECTION_PROFILE": "",
                "COLLECTION_PROXY": "",
                "COLLECTION_BROWSER_HEADLESS": "1",
            },
        ),
    ):
        yield


def collect(destination: Path, *arguments: str) -> dict[str, object]:
    output = StringIO()
    call_command("collect", *arguments, "--corpus-dir", str(destination), stdout=output)
    result: dict[str, object] = json.loads(output.getvalue())
    return result


def read_json(path: Path) -> dict[str, object]:
    result: dict[str, object] = json.loads(path.read_text())
    return result


class CorpusTests(TestCase):
    def test_collect_caps_reads_and_resumes_without_replacing_first_snapshot(
        self,
    ) -> None:
        # Arrange
        route = route_for(SAMARA_REGION_ID, "sale", APARTMENT_TYPE_ID)
        assert route is not None
        first_url, offer, product = projection()
        first_id = str(offer["id"])
        second_id = str(int(first_id) + 1)
        second_url = first_url.replace(first_id, second_id)
        foreign_id = str(int(first_id) + 2)
        foreign_url = first_url.replace(first_id, foreign_id)
        offer["description"] = (
            "Ремонт. Звоните +7\u00a0999\u00a0123-45-67, tel:9991234567"
        )
        offer["phone"] = "secret-field"
        invalid_offer = {
            **offer,
            "id": int(second_id),
            "cianId": int(second_id),
            "totalArea": "47.211",
        }
        foreign_offer = {
            **offer,
            "id": int(foreign_id),
            "cianId": int(foreign_id),
            "geo": {
                "address": [
                    {
                        "id": 4609,
                        "type": "location",
                        "locationTypeId": 2,
                        "fullName": "Саратовская область",
                    }
                ]
            },
        }
        listing: dict[str, object] = {
            "results": {
                "jsonQuery": {
                    "_type": "flatsale",
                    "engine_version": {"type": "term", "value": 2},
                    "region": {"type": "terms", "value": [4608]},
                },
                "offers": [
                    {"id": int(key)} for key in (first_id, second_id, foreign_id)
                ],
                "totalOffers": 3,
            }
        }
        responses = {
            route.start: (
                f"<script>{script(listing)}</script>"
                f'<a href="{first_url}">Первая</a><a href="{second_url}">Вторая</a>'
                f'<a href="{foreign_url}">Третья</a>'
            ),
            first_url: projection_html(offer, product, "12 345 678 ₽"),
            second_url: projection_html(invalid_offer, {}),
            foreign_url: projection_html(foreign_offer, {}),
        }
        visits: list[str] = []
        scope = (
            "--source",
            "cian",
            "--region",
            str(SAMARA_REGION_ID),
            "--property-type",
            "apartment",
            "--deal",
            "sale",
        )

        with TemporaryDirectory() as directory, replay(responses, visits):
            corpus = Path(directory) / "fresh"
            first = corpus / "cian" / first_id / "raw.json"
            second = corpus / "cian" / second_id

            # Act
            first_run = collect(corpus, *scope)
            original = first.read_text()
            reads_after_limit = [url for url in visits if url != route.start]
            resumed = collect(corpus, *scope)
            repeated = collect(corpus, *scope)

            # Assert
            self.assertEqual(reads_after_limit, [first_url])
            self.assertEqual(
                [url for url in visits if url != route.start],
                [first_url, second_url, foreign_url],
            )
            self.assertEqual(first_run["issues"], {"stopped_by_limit": 1})
            self.assertEqual(
                [first_run["saved"], resumed["saved"], repeated["saved"]], [1, 1, 0]
            )
            self.assertEqual(first.read_text(), original)
            self.assertEqual(repeated["issues"], {"card_outside_scope": 1})
            self.assertFalse((corpus / "cian" / foreign_id).exists())
            self.assertNotIn(
                "999", json.loads(original)["published_fields"]["description"]
            )
            self.assertNotIn("secret-field", original)
            self.assertIn(
                "Ремонт.", json.loads(original)["published_fields"]["description"]
            )
            normalized = read_json(first.with_name("normalized.json"))
            description = normalized["description"]
            self.assertIsInstance(description, str)
            assert isinstance(description, str)
            self.assertIn("Ремонт.", description)
            self.assertNotIn("999", description)
            self.assertEqual(
                json.loads(original)["published_fields"]["offerPrice"], "12 345 678 ₽"
            )
            self.assertEqual(json.loads(original)["listing_page"], 1)
            self.assertEqual(json.loads(original)["url"], first_url)
            self.assertEqual(
                json.loads(original)["published_fields"]["photos"], product["image"]
            )
            self.assertEqual(
                json.loads((second / "raw.json").read_text())["external_id"], second_id
            )
            rejected = json.loads((second / "normalized.json").read_text())
            self.assertEqual(
                (rejected["state"], rejected["reason"]),
                ("rejected", "invalid_precision"),
            )

    def test_avito_card_and_domclick_search_share_one_corpus(self) -> None:
        # Arrange
        avito_card = avito_fixture.projection("resale")
        avito_url = avito_card["url"]
        avito_key = avito.card_key(avito_url)
        room_url = domclick_fixture.ROOM
        room_key = domclick.card_key(room_url)
        assert avito_key is not None and room_key is not None
        (route,) = routes_for(SAMARA_REGION_ID, "sale", ROOM_TYPE_ID)
        state: dict[str, object] = {
            "search": {
                "status": "loaded",
                "pagination": {"offset": 0, "limit": 20},
                "snippetsCount": 1,
                "filters": {
                    "applied": {
                        "aids": [route.source_region],
                        "deal_type": "sale",
                        "category": "living",
                        "offer_type": ["room"],
                    }
                },
            }
        }
        listing = domclick_fixture.ssr(state)
        proposal = domclick_fixture.ssr(
            {"productCard": domclick_fixture.native_proposal()}
        )
        responses = {
            avito_url: avito_fixture.projection_html(avito_card),
            route.start: (
                f"<script>{listing}</script>"
                f'<div><section><a href="{room_url}">Комната</a></section>'
                '<div class="pgnt-root-example"><button data-e2e-id="paginate-next-btn" disabled>'
                "Следующая</button></div></div>"
            ),
            room_url: f"<script>{proposal}</script>",
        }
        visits: list[str] = []
        scope = (
            "--source",
            "domclick",
            "--region",
            str(SAMARA_REGION_ID),
            "--property-type",
            "room",
            "--deal",
            "sale",
        )
        with TemporaryDirectory() as directory, replay(responses, visits):
            root = Path(directory)
            old = root / "old"
            old.mkdir()
            sentinel = old / "cards-raw-v1.ndjson"
            sentinel.write_text("previous corpus")
            corpus = root / "fresh"

            # Act
            card = collect(corpus, avito_url)
            repeated = collect(corpus, avito_url)
            regional = collect(corpus, *scope)

            # Assert
            self.assertEqual(
                [card["saved"], repeated["saved"], regional["saved"]], [1, 0, 1]
            )
            self.assertEqual(repeated["outcome"], "already_saved")
            self.assertEqual(visits.count(avito_url), 1)
            self.assertEqual(sentinel.read_text(), "previous corpus")
            avito_dir = corpus / "avito" / avito_key
            domclick_dir = corpus / "domclick" / quote(room_key, safe="")
            avito_raw = read_json(avito_dir / "raw.json")
            domclick_raw = read_json(domclick_dir / "raw.json")
            self.assertIsNone(avito_raw["listing_scope"])
            self.assertEqual(domclick_raw["listing_page"], 1)
            self.assertEqual(
                read_json(avito_dir / "normalized.json")["state"], "active"
            )
            self.assertEqual(
                read_json(domclick_dir / "normalized.json")["state"], "active"
            )

    def test_corpus_rejects_a_broker_before_opening_a_browser(self) -> None:
        # Arrange
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "fresh"
            url = avito_fixture.projection("resale")["url"]

            # Act / Assert
            with self.assertRaisesRegex(
                CommandError, "^corpus_does_not_use_captcha_broker$"
            ):
                collect(destination, url, "--captcha-broker", str(destination))
            self.assertFalse(destination.exists())
