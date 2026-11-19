from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.test import TestCase

from app.catalog.contracts import ActiveSnapshot
from app.catalog.models import Ad, PriceHistory, Property
from app.catalog.publications import accept_snapshot
from app.catalog.reference_defaults import HOUSE_TYPE_ID, SAMARA_REGION_ID

TIME = datetime(2026, 11, 17, tzinfo=UTC)
HOUSE = "Самарская область, Самара, СНТ Луч, ул. Садовая, дом 7"


def snapshot(
    key: str,
    address: str | None,
    *,
    source: str = "cian",
    time: datetime = TIME,
    area: Decimal | None = Decimal("80"),
    floors: int | None = 2,
    land: Decimal | None = None,
) -> ActiveSnapshot:
    return {
        "state": "active",
        "source_code": source,
        "external_id": key,
        "observed_at": time,
        "url": f"https://example.test/offer/{key}",
        "region_id": SAMARA_REGION_ID,
        "property_type_id": HOUSE_TYPE_ID,
        "deal_type": "sale",
        "market_segment": None,
        "address": address,
        "latitude": None,
        "longitude": None,
        "area_m2": area,
        "rooms": None,
        "floor": None,
        "floors_total": floors,
        "land_area_m2": land,
        "description": None,
        "price": {
            "status": "known",
            "price_kind": "listed",
            "price_rub": Decimal(100),
            "currency": "RUB",
        },
        "photos": {"status": "known", "urls": ["https://example.test/first.jpg"]},
    }


class HouseMatchingTests(TestCase):
    def test_concrete_house_and_block_are_distinct_units(self) -> None:
        # Arrange
        first = snapshot("house", HOUSE, area=None, floors=None)
        bad_block = snapshot("no-id", f"{HOUSE}, блок")
        valid_block = snapshot("block", f"{HOUSE}, блок 2")

        # Act
        whole = accept_snapshot(first)
        rejected = accept_snapshot(bad_block)
        block = accept_snapshot(valid_block)

        # Assert
        self.assertEqual(whole.outcome, "created")
        self.assertEqual(rejected.reason, "unconfirmed_house_block")
        self.assertEqual(block.outcome, "created")
        self.assertNotEqual(whole.property_id, block.property_id)
        assert block.property_id is not None
        saved_block = Property.objects.get(pk=block.property_id)
        self.assertEqual(saved_block.address, f"{HOUSE}, блок 2")
        self.assertIsNone(saved_block.floor)
        self.assertEqual(Ad.objects.count(), 2)
        self.assertEqual(PriceHistory.objects.count(), 2)

    def test_only_same_block_and_compatible_facts_attach(self) -> None:
        # Arrange
        original = accept_snapshot(
            snapshot("original", f"{HOUSE}, секция А", land=Decimal(600))
        )
        alternative = "Самара, СНТ Луч, Садовая улица, 7, секция а"
        same_block = snapshot("same", alternative, source="domclick")
        same_block["rooms"] = 3
        others = (
            snapshot("other-block", f"{HOUSE}, секция Б"),
            snapshot("whole", HOUSE),
            snapshot("no-area", f"{HOUSE}, секция А", area=None),
            snapshot("no-floors", f"{HOUSE}, секция А", floors=None),
            snapshot("other-land", f"{HOUSE}, секция А", land=Decimal(700)),
            snapshot("other-area", f"{HOUSE}, секция А", area=Decimal("80.01")),
            snapshot("other-territory", f"{HOUSE.replace('Луч', 'Сад')}, секция А"),
        )

        # Act
        joined = accept_snapshot(same_block)
        separate = [accept_snapshot(other) for other in others]

        # Assert
        self.assertEqual(original.property_id, joined.property_id)
        self.assertNotIn(original.property_id, [item.property_id for item in separate])
        self.assertEqual(len({item.property_id for item in separate}), len(others))
        assert original.property_id is not None
        saved = Property.objects.get(pk=original.property_id)
        self.assertEqual(saved.address, f"{HOUSE}, секция А")
        self.assertEqual(saved.rooms, 3)
        self.assertEqual(saved.land_area_m2, Decimal(600))

    def test_plot_and_bare_number_are_not_house_identifiers(self) -> None:
        # Arrange
        territory = "Самара, СНТ Луч"
        candidate = accept_snapshot(snapshot("orig", f"{territory}, дом 17"))
        addresses = (
            f"{territory}, участок 17",
            f"{territory}, №17",
            f"{territory}, 17",
            "Самара, дом 17",
        )

        # Act
        same = accept_snapshot(snapshot("same", f"{territory}, д. 17", source="avito"))
        separate = [
            accept_snapshot(snapshot(f"partial-{index}", address))
            for index, address in enumerate(addresses, 1)
        ]

        # Assert
        self.assertEqual(same.property_id, candidate.property_id)
        self.assertEqual(len({item.property_id for item in separate}), len(addresses))
        self.assertNotIn(candidate.property_id, [item.property_id for item in separate])

    def test_several_positive_candidates_are_not_chosen_by_id(self) -> None:
        # Arrange
        first = accept_snapshot(snapshot("first", HOUSE))
        second = accept_snapshot(snapshot("second", HOUSE, area=None))
        accept_snapshot(snapshot("second", HOUSE, time=TIME + timedelta(minutes=1)))

        # Act
        third = accept_snapshot(snapshot("third", HOUSE, source="domclick"))

        # Assert
        self.assertEqual(
            len({first.property_id, second.property_id, third.property_id}), 3
        )

    def test_repeat_preserves_bytes_and_timestamp_on_address_conflict(self) -> None:
        # Arrange
        saved_address = f"{HOUSE}, блок 2"
        first = accept_snapshot(snapshot("same-key", saved_address, land=Decimal(600)))
        conflicts = (
            f"{HOUSE}, блок 3",
            HOUSE.replace("дом 7", "дом 8"),
            "Самара, блок 3",
        )
        newer = TIME + timedelta(minutes=1)

        # Act
        expired = accept_snapshot(
            snapshot("same-key", conflicts[0], time=TIME - timedelta(minutes=1))
        )
        equal = accept_snapshot(snapshot("same-key", conflicts[0]))
        rejected = []
        for address in conflicts:
            changed = snapshot("same-key", address, time=newer)
            changed["photos"] = {"status": "known", "urls": []}
            changed["price"] = {
                "status": "known",
                "price_kind": "listed",
                "price_rub": Decimal(250),
                "currency": "RUB",
            }
            rejected.append(accept_snapshot(changed))

        # Assert
        self.assertEqual(expired.outcome, "stale")
        self.assertEqual(equal.outcome, "ignored")
        self.assertEqual(
            [item.outcome for item in rejected], ["conflict"] * len(conflicts)
        )
        assert first.ad_id is not None
        ad = Ad.objects.get(pk=first.ad_id)
        self.assertEqual(ad.property_id, first.property_id)
        self.assertEqual(ad.observed_at, TIME)
        self.assertEqual(ad.photos, ["https://example.test/first.jpg"])
        self.assertEqual(ad.property.address, saved_address)
        self.assertEqual(PriceHistory.objects.get(ad=ad).price_rub, Decimal(100))

    def test_repeat_needs_a_published_chain_for_address_patch(self) -> None:
        # Arrange
        place = "Самара, СНТ Луч"
        first = accept_snapshot(snapshot("repeat", f"{place}, участок 17", area=None))
        incomplete = snapshot(
            "repeat", f"{place}, дом 7", time=TIME + timedelta(minutes=1)
        )
        complete = snapshot(
            "repeat",
            f"{place}, дом 7, участок 17",
            time=TIME + timedelta(minutes=2),
            land=Decimal(600),
        )
        later = snapshot("repeat", place, time=TIME + timedelta(minutes=3), area=None)

        # Act
        accept_snapshot(incomplete)

        # Assert
        assert first.property_id is not None
        saved = Property.objects.get(pk=first.property_id)
        self.assertEqual(saved.address, f"{place}, участок 17")

        # Act
        accept_snapshot(complete)
        accept_snapshot(later)

        # Assert
        saved.refresh_from_db()
        self.assertEqual(saved.address, complete["address"])
        self.assertEqual(saved.area_m2, Decimal(80))
        self.assertEqual(saved.land_area_m2, Decimal(600))

    def test_repeated_block_keeps_omitted_scope_and_whole_cannot_become_block(
        self,
    ) -> None:
        # Arrange
        block_address = f"{HOUSE}, блок 2"
        block = accept_snapshot(snapshot("block", block_address))
        whole = accept_snapshot(snapshot("whole", HOUSE))
        newer = TIME + timedelta(minutes=1)

        # Act
        omitted = accept_snapshot(snapshot("block", HOUSE, time=newer))
        changed = accept_snapshot(snapshot("whole", block_address, time=newer))

        # Assert
        self.assertEqual(omitted.outcome, "unchanged")
        self.assertEqual(changed.outcome, "conflict")
        assert block.property_id is not None and whole.property_id is not None
        self.assertEqual(
            Property.objects.get(pk=block.property_id).address, block_address
        )
        self.assertEqual(Property.objects.get(pk=whole.property_id).address, HOUSE)
