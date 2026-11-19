from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.test import TestCase

from app.catalog.contracts import ActiveSnapshot
from app.catalog.models import Ad, PriceHistory, Property
from app.catalog.publications import accept_snapshot
from app.catalog.reference_defaults import HOUSE_TYPE_ID, REGION_NAMES, SAMARA_REGION_ID

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
    region_id: int = SAMARA_REGION_ID,
    price: Decimal = Decimal(100),
) -> ActiveSnapshot:
    return {
        "state": "active",
        "source_code": source,
        "external_id": key,
        "observed_at": time,
        "url": f"https://example.test/offer/{key}",
        "region_id": region_id,
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
            "price_rub": price,
            "currency": "RUB",
        },
        "photos": {"status": "known", "urls": ["https://example.test/first.jpg"]},
    }


class HouseMatchingTests(TestCase):
    def test_concrete_house_and_block_are_distinct_units(self) -> None:
        # Arrange
        first = snapshot("house", HOUSE, area=None, floors=None)
        bad_blocks = (
            snapshot("no-id", f"{HOUSE}, блок"),
            snapshot("no-inline-id", f"{HOUSE} блок"),
            snapshot("no-building", "Самара, СНТ Луч, улица Садовая блок 2"),
            snapshot("territory-no-id", "Самара, СНТ Луч дом 7 блок"),
        )
        valid_block = snapshot("block", f"{HOUSE} блок 2")

        # Act
        whole = accept_snapshot(first)
        rejected = [accept_snapshot(item) for item in bad_blocks]
        block = accept_snapshot(valid_block)

        # Assert
        self.assertEqual(whole.outcome, "created")
        self.assertEqual(
            [item.reason for item in rejected],
            ["unconfirmed_house_block"] * len(bad_blocks),
        )
        self.assertEqual(block.outcome, "created")
        self.assertNotEqual(whole.property_id, block.property_id)
        assert block.property_id is not None
        saved_block = Property.objects.get(pk=block.property_id)
        self.assertEqual(saved_block.address, f"{HOUSE} блок 2")
        self.assertIsNone(saved_block.floor)
        self.assertEqual(Ad.objects.count(), 2)
        self.assertEqual(PriceHistory.objects.count(), 2)

    def test_only_same_block_and_compatible_facts_attach(self) -> None:
        # Arrange
        original = accept_snapshot(
            snapshot("original", f"{HOUSE}, секция А", land=Decimal(600))
        )
        alternative = "Самара, СНТ Луч, Садовая ул. дом 7 секция а"
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

    def test_inconsistent_locality_cannot_determine_the_same_house(self) -> None:
        addresses = (
            "город Самара, город Тольятти, улица Садовая, дом 7",
            "Самара, Тольятти, СНТ Луч, улица Садовая, дом 7",
            "Самара, СНТ Луч, улица Садовая, Тольятти, дом 7",
            HOUSE.replace("Самарская область", "Саратовская обл."),
            HOUSE.replace("Самарская область", "Неизвестная область"),
        )
        for index, address in enumerate(addresses):
            with self.subTest(address=address):
                # Arrange
                key = f"inconsistent-{index}"
                first = accept_snapshot(snapshot(key, address))

                # Act
                second = accept_snapshot(snapshot(key, address, source="domclick"))

                # Assert
                self.assertEqual(first.outcome, "created")
                self.assertEqual(second.outcome, "created")
                self.assertNotEqual(first.property_id, second.property_id)

    def test_repeat_and_attach_use_the_same_seeded_subject_identity(self) -> None:
        # Arrange
        region_id = 19
        location = "Казань, ул. Садовая, дом 7"
        address = f"{REGION_NAMES[region_id]}, {location}"
        first = accept_snapshot(
            snapshot("registered-subject", address, region_id=region_id)
        )
        newer = TIME + timedelta(minutes=1)
        alternate = snapshot(
            "registered-subject",
            f"Республика Татарстан, {location}",
            time=newer,
            region_id=region_id,
            price=Decimal(250),
        )
        alias = snapshot(
            "subject-alias",
            f"Татарстан, {location}",
            source="domclick",
            region_id=region_id,
        )
        unknown = snapshot(
            "registered-subject",
            f"Неизвестная область, {location}",
            time=newer + timedelta(minutes=1),
            region_id=region_id,
            price=Decimal(250),
        )

        # Act
        compatible = accept_snapshot(alternate)
        joined = accept_snapshot(alias)
        partial = accept_snapshot(unknown)

        # Assert
        self.assertEqual(compatible.outcome, "updated")
        self.assertEqual(partial.outcome, "unchanged")
        self.assertEqual(
            [compatible.property_id, joined.property_id, partial.property_id],
            [first.property_id] * 3,
        )
        assert first.ad_id is not None
        ad = Ad.objects.get(pk=first.ad_id)
        self.assertEqual(ad.property.address, address)
        self.assertEqual(ad.observed_at, unknown["observed_at"])
        self.assertEqual(PriceHistory.objects.filter(ad=ad).count(), 2)

    def test_territory_and_inline_block_retain_separate_address_roles(self) -> None:
        # Arrange
        inline = "Самарская обл., Самара, территория СНТ Луч дом 7 блок 2"
        first = accept_snapshot(snapshot("inline", inline))
        separate = "Самара, СНТ Луч, дом 7, блок 2"

        # Act
        same = accept_snapshot(snapshot("separate", separate, source="domclick"))

        # Assert
        self.assertEqual(first.outcome, "created")
        self.assertEqual(same.property_id, first.property_id)
        assert first.property_id is not None
        self.assertEqual(Property.objects.get(pk=first.property_id).address, inline)

    def test_inline_plot_and_multiple_house_numbers_do_not_attach(self) -> None:
        street = "Самара, СНТ Луч, улица Садовая"
        suffixes = (
            "участок 7",
            "дом 7 дом 8",
            "дом 7 участок 17 дом 8",
            "дом 7, дом 7",
        )
        for index, suffix in enumerate(suffixes, 1):
            with self.subTest(suffix=suffix):
                # Arrange
                address = f"{street} {suffix}"
                key = f"ambiguous-{index}"
                first = accept_snapshot(snapshot(key, address))

                # Act
                second = accept_snapshot(snapshot(key, address, source="domclick"))

                # Assert
                self.assertEqual(first.outcome, "created")
                self.assertEqual(second.outcome, "created")
                self.assertNotEqual(first.property_id, second.property_id)

    def test_street_with_a_number_in_its_name_retains_the_building_number(self) -> None:
        # Arrange
        first = accept_snapshot(
            snapshot("first", "Самара, СНТ Луч, улица 8 Марта, дом 7 блок 2")
        )
        alternative = "Самара, территория СНТ Луч, 8 Марта ул. дом 7, блок 2"

        # Act
        second = accept_snapshot(snapshot("second", alternative, source="domclick"))

        # Assert
        self.assertEqual(first.outcome, "created")
        self.assertEqual(second.property_id, first.property_id)

    def test_repeat_preserves_bytes_and_timestamp_on_address_conflict(self) -> None:
        # Arrange
        saved_address = (
            HOUSE.replace("Самара,", "Приволжский район, Самара,") + ", блок 2"
        )
        first = accept_snapshot(snapshot("same-key", saved_address, land=Decimal(600)))
        conflicts = (
            f"{HOUSE}, блок 3",
            HOUSE.replace("дом 7", "дом 8"),
            "Самара, блок 3",
            "Самара, СНТ Луч, ул. Полевая",
            "город Тольятти, улица Садовая",
            "Самара, улица Садовая, 8, СНТ Луч",
            "Самара, территория СНТ Сад",
            "Кинельский район, Самара, СНТ Луч",
            saved_address.replace("Самарская область", "Саратовская область"),
            saved_address.replace("дом 7", "дом 9, дом 9"),
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

    def test_repeat_with_competing_numbers_retains_published_address(self) -> None:
        # Arrange
        first = accept_snapshot(snapshot("ambiguous-repeat", HOUSE))
        address = HOUSE.replace("дом 7", "дом 9, дом 7")
        newer = TIME + timedelta(minutes=1)

        # Act
        repeat = accept_snapshot(snapshot("ambiguous-repeat", address, time=newer))

        # Assert
        self.assertEqual(repeat.outcome, "unchanged")
        self.assertEqual(repeat.property_id, first.property_id)
        assert first.ad_id is not None
        ad = Ad.objects.get(pk=first.ad_id)
        self.assertEqual(ad.observed_at, newer)
        self.assertEqual(ad.property.address, HOUSE)
        self.assertEqual(PriceHistory.objects.filter(ad=ad).count(), 1)

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

    def test_repeat_with_structure_omits_only_the_house_number(self) -> None:
        # Arrange
        first_address = f"{HOUSE}, строение 2"
        first = accept_snapshot(snapshot("structure", first_address))
        partial = HOUSE.removesuffix(", дом 7")
        retained = f"{partial}, строение 2"
        changed = f"{partial}, строение 3"
        newer = TIME + timedelta(minutes=1)

        # Act
        same = accept_snapshot(snapshot("structure", retained, time=newer))
        conflict = accept_snapshot(
            snapshot("structure", changed, time=newer + timedelta(minutes=1))
        )

        # Assert
        self.assertEqual(same.outcome, "unchanged")
        self.assertEqual(same.property_id, first.property_id)
        self.assertEqual(conflict.outcome, "conflict")
        assert first.ad_id is not None
        stored = Ad.objects.get(pk=first.ad_id)
        self.assertEqual(stored.observed_at, newer)
        self.assertEqual(stored.property.address, first_address)
        self.assertEqual(PriceHistory.objects.filter(ad=stored).count(), 1)

    def test_repeated_block_keeps_omitted_scope_and_whole_cannot_become_block(
        self,
    ) -> None:
        # Arrange
        block_address = f"{HOUSE}, блок 2"
        block = accept_snapshot(snapshot("block", block_address))
        whole = accept_snapshot(snapshot("whole", HOUSE))
        newer = TIME + timedelta(minutes=1)
        without_block = "Самара, Садовая ул. дом 7"

        # Act
        omitted = accept_snapshot(snapshot("block", without_block, time=newer))
        changed = accept_snapshot(snapshot("whole", block_address, time=newer))

        # Assert
        self.assertEqual(omitted.outcome, "unchanged")
        self.assertEqual(changed.outcome, "conflict")
        assert block.property_id is not None and whole.property_id is not None
        self.assertEqual(
            Property.objects.get(pk=block.property_id).address, block_address
        )
        self.assertEqual(Property.objects.get(pk=whole.property_id).address, HOUSE)

    def test_repeat_of_an_identified_block_does_not_require_attach_completeness(
        self,
    ) -> None:
        # Arrange
        address = HOUSE.replace("Самара,", "Самара, Советский район,")
        address += ", корпус А, блок 2"
        first = accept_snapshot(snapshot("repeated-block", address))
        duplicate_house = address.replace("дом 7", "дом 7, дом 7")
        reduced = duplicate_house.replace("Советский район, ", "").replace(
            "корпус А, ", ""
        )
        newer = TIME + timedelta(minutes=1)
        repeat = snapshot("repeated-block", reduced, time=newer, price=Decimal(250))
        incomplete = (
            address.replace("дом 7", "дом 7, дом 8"),
            address.replace("дом 7", "строение 7, строение 7"),
            address.replace("блок 2", "блок"),
            address.replace("блок 2", "блок 2, блок 3"),
        )

        # Act
        compatible = accept_snapshot(repeat)
        separate = accept_snapshot(snapshot("new-block", reduced))
        rejected = [
            accept_snapshot(
                snapshot("repeated-block", item, time=newer + timedelta(minutes=1))
            )
            for item in incomplete
        ]

        # Assert
        self.assertEqual(compatible.outcome, "updated")
        self.assertEqual(compatible.property_id, first.property_id)
        self.assertEqual(separate.reason, "unconfirmed_house_block")
        self.assertEqual(
            [item.reason for item in rejected],
            ["unconfirmed_house_block"] * len(incomplete),
        )
        assert first.ad_id is not None
        ad = Ad.objects.get(pk=first.ad_id)
        self.assertEqual(ad.observed_at, newer)
        self.assertEqual(ad.property.address, address)
        self.assertEqual(Property.objects.count(), 1)
        self.assertEqual(Ad.objects.count(), 1)
        self.assertEqual(
            set(PriceHistory.objects.filter(ad=ad).values_list("price_rub", flat=True)),
            {Decimal(100), Decimal(250)},
        )

    def test_repeat_compares_established_location_roles_independently(self) -> None:
        for key, original_city, repeated_city in (
            ("bare-city", "Самара", "г. Самара"),
            ("typed-city", "г. Самара", "Самара"),
        ):
            with self.subTest(original_city=original_city):
                # Arrange
                street = "СНТ Луч, ул. Садовая"
                address = f"{original_city}, Советский район, {street}, дом 7, блок 2"
                first = accept_snapshot(snapshot(key, address))
                same = address.replace(original_city, repeated_city, 1)
                partial = f"Приволжский район, {repeated_city}, {street}"
                redundant = f"{partial}, дом 7, дом 7, блок 2"
                changed = same.replace("Советский район", "Ленинский район")
                newer = TIME + timedelta(minutes=2)

                # Act
                same_locality = accept_snapshot(
                    snapshot(
                        key, same, time=newer - timedelta(minutes=1), price=Decimal(200)
                    )
                )
                compatible = accept_snapshot(
                    snapshot(key, redundant, time=newer, price=Decimal(250))
                )
                conflict = accept_snapshot(
                    snapshot(key, changed, time=newer + timedelta(minutes=1))
                )
                cross_source = accept_snapshot(snapshot(key, same, source="domclick"))

                # Assert
                self.assertEqual(same_locality.outcome, "updated")
                self.assertEqual(compatible.outcome, "updated")
                self.assertEqual(compatible.property_id, first.property_id)
                self.assertEqual(conflict.outcome, "conflict")
                self.assertNotEqual(cross_source.property_id, first.property_id)
                assert first.ad_id is not None
                ad = Ad.objects.get(pk=first.ad_id)
                self.assertEqual(ad.observed_at, newer)
                self.assertEqual(ad.property.address, address)
                prices = PriceHistory.objects.filter(ad=ad).values_list(
                    "price_rub", flat=True
                )
                self.assertEqual(
                    set(prices), {Decimal(100), Decimal(200), Decimal(250)}
                )

    def test_repeat_detects_explicit_type_change_of_known_nested_settlement(
        self,
    ) -> None:
        for original_kind, equivalent_kind, changed_kind in (
            ("посёлок", "пос.", "деревня"),
            ("деревня", "деревня", "посёлок"),
        ):
            with self.subTest(original_kind=original_kind):
                # Arrange
                tail = "СНТ Луч, ул. Садовая, дом 7, блок 2"
                original = f"г. Самара, {original_kind} Новый, {tail}"
                equivalent = f"г. Самара, {equivalent_kind} Новый, {tail}"
                changed = f"г. Самара, {changed_kind} Новый, {tail}"
                later = TIME + timedelta(minutes=2)
                first = accept_snapshot(snapshot(original_kind, original))

                # Act
                same_place = accept_snapshot(
                    snapshot(
                        original_kind,
                        equivalent,
                        time=later - timedelta(minutes=1),
                        price=Decimal(200),
                    )
                )
                omitted = accept_snapshot(
                    snapshot(
                        original_kind,
                        f"г. Самара, {tail}",
                        time=later,
                        price=Decimal(250),
                    )
                )
                conflict = accept_snapshot(
                    snapshot(
                        original_kind,
                        changed,
                        time=later + timedelta(minutes=1),
                        price=Decimal(400),
                    )
                )

                # Assert
                self.assertEqual(same_place.outcome, "updated")
                self.assertEqual(omitted.outcome, "updated")
                self.assertEqual(omitted.property_id, first.property_id)
                self.assertEqual(conflict.outcome, "conflict")
                assert first.ad_id is not None
                ad = Ad.objects.get(pk=first.ad_id)
                self.assertEqual(ad.observed_at, later)
                self.assertEqual(ad.property.address, original)
                self.assertEqual(
                    set(
                        PriceHistory.objects.filter(ad=ad).values_list(
                            "price_rub", flat=True
                        )
                    ),
                    {Decimal(100), Decimal(200), Decimal(250)},
                )

    def test_repeat_does_not_assign_type_from_repeated_settlement_name(self) -> None:
        # Arrange
        tail = "СНТ Луч, ул. Садовая, дом 7, блок 2"
        original = f"г. Самара, посёлок Новый, деревня Новый, {tail}"
        first = accept_snapshot(snapshot("ambiguous-place-type", original))

        # Act
        repeated = accept_snapshot(
            snapshot(
                "ambiguous-place-type",
                f"г. Самара, посёлок Новый, {tail}",
                time=TIME + timedelta(minutes=1),
                price=Decimal(250),
            )
        )

        # Assert
        self.assertEqual(repeated.outcome, "updated")
        self.assertEqual(repeated.property_id, first.property_id)
        assert first.ad_id is not None
        ad = Ad.objects.get(pk=first.ad_id)
        self.assertEqual(ad.property.address, original)
        self.assertEqual(
            set(PriceHistory.objects.filter(ad=ad).values_list("price_rub", flat=True)),
            {Decimal(100), Decimal(250)},
        )
