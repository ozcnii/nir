"""Card projections and deliberately invalid inputs for the Domclick adapter."""

from decimal import Decimal
from functools import partial
from typing import cast
from unittest import TestCase

from playwright.sync_api import Route, sync_playwright

from app.catalog.contracts import Snapshot
from app.collection.collectors.domclick import (
    DailyListingQuote,
    card_key,
    parse_card,
    read_card,
    read_daily_listing_quote,
)
from app.collection.normalization import RejectedCard
from app.collection.tests.domclick_fixture import (
    DAILY,
    DAILY_PHOTO,
    HOUSE,
    LISTING,
    OBSERVED,
    PARTNER,
    ROOM,
    daily_listing_parts,
    daily_projection,
    daily_ssr,
    native_proposal,
    nuxt_script,
    partner_projection,
    ssr,
)


def read_native(
    url: str, data: dict[str, object], *, removed: bool = False
) -> Snapshot:
    return parse_card(
        url,
        OBSERVED,
        [ssr({"productCard": data})],
        [],
        loaded_url=url,
        removed_marker=removed,
    )


class DomclickTests(TestCase):
    def test_native_room_facts_keep_offered_count_and_reject_conflict(self) -> None:
        # Arrange
        data = native_proposal()
        info = cast(dict[str, object], data["objectInfo"])
        info["description"] = "Площадь кухни 10 м²."
        info["flatNumber"] = "№25"
        address = cast(dict[str, object], data["address"])
        address["displayName"] = "Россия, Самара, Солнечная улица, 7, кв 25, комната 2"

        # Act
        single = read_native(ROOM, data)
        unknown = read_native(ROOM, {**data, "roomsOffered": None})
        multiple = read_native(ROOM, {**data, "roomsOffered": 2})

        # Assert
        assert single["state"] == unknown["state"] == multiple["state"] == "active"
        self.assertEqual(single["rooms"], 1)
        self.assertTrue((single["address"] or "").endswith("кв 25, комната 2"))
        self.assertIsNone(unknown["rooms"])
        self.assertEqual(multiple["rooms"], 2)
        self.assertIsNone(multiple["area_m2"])

        # Arrange
        info["roomArea"] = 9.0
        info["description"] = (
            "Доля: комната № 2 на фото, продаётся только 1/2 квартиры."
        )
        with self.assertRaisesRegex(RejectedCard, "^unconfirmed_room_unit$"):
            # Act
            read_native(ROOM, data)

        # Assert: the exact failure is validated by assertRaisesRegex.

        # Arrange
        info["description"] = "Продаётся комната № 2 в квартире № 25 площадью 9,2 м²."
        with self.assertRaises(RejectedCard) as rejected:
            # Act
            read_native(ROOM, data)

        # Assert
        self.assertEqual(str(rejected.exception), "conflicting_room_area")

    def test_native_room_keeps_the_offered_room_and_seller_text(self) -> None:
        # Arrange
        data = native_proposal()
        script = ssr({"productCard": data, "other": None}).replace(
            '"other": null', '"other": undefined'
        )

        # Act
        result = parse_card(ROOM, OBSERVED, [script], [], loaded_url=ROOM)

        # Assert
        self.assertEqual(
            result,
            {
                "source_code": "domclick",
                "external_id": "/card/sale__room__2071752356",
                "observed_at": OBSERVED,
                "state": "active",
                "url": ROOM,
                "region_id": 68,
                "property_type_id": 2,
                "deal_type": "sale",
                "market_segment": None,
                "address": "Самарская область, Самара, Солнечная улица, 7",
                "latitude": Decimal("53.2406910"),
                "longitude": Decimal("50.1902950"),
                "area_m2": Decimal("9.20"),
                "rooms": 1,
                "floor": 1,
                "floors_total": 9,
                "land_area_m2": None,
                "description": "Текст продавца: undefined — сохранить.\nРазмер 9,2 м².",
                "price": {
                    "status": "known",
                    "price_rub": Decimal("1400000.00"),
                    "price_kind": "listed",
                    "currency": "RUB",
                },
                "photos": {"status": "unavailable"},
            },
        )

    def test_explicit_room_set_and_area_claims_follow_the_offered_unit(self) -> None:
        # Arrange: one room in structure, then the same claim with unknown count.
        data = native_proposal()
        info = cast(dict[str, object], data["objectInfo"])
        info["description"] = "Прекрасный район. Продам две комнаты."

        # Act
        with self.assertRaises(RejectedCard) as rejected:
            read_native(ROOM, data)
        set_snapshot = read_native(ROOM, {**data, "roomsOffered": None})

        # Assert
        self.assertEqual(str(rejected.exception), "conflicting_room_count")
        assert set_snapshot["state"] == "active"
        self.assertEqual(set_snapshot["rooms"], 2)
        self.assertIsNone(set_snapshot["area_m2"])

        # Arrange: distinguish directly offered area after introduction, a
        # directly offered share, and an unrelated nearby room measurement.
        cases = (
            ("Остановка рядом. Продаю комнату 9,0 м².", "conflicting_room_area"),
            ("Остановка рядом, продам комнату 9,0 м².", "conflicting_room_area"),
            ("Остановка рядом, продаётся комната 9,0 м² в соседней квартире.", None),
            (
                "Подробности по телефону. Продаётся невыделенная доля в квартире.",
                "unconfirmed_room_unit",
            ),
            ("Продаётся комната 9,0 м² в соседней квартире.", None),
        )
        for description, problem in cases:
            with self.subTest(description=description):
                info["description"] = description
                if problem is None:
                    # Act
                    accepted = read_native(ROOM, data)

                    # Assert
                    assert accepted["state"] == "active"
                    self.assertEqual(accepted["area_m2"], Decimal("9.20"))
                else:
                    # Act
                    with self.assertRaises(RejectedCard) as rejected:
                        read_native(ROOM, data)

                    # Assert
                    self.assertEqual(str(rejected.exception), problem)

    def test_numbered_room_set_uses_only_explicit_total_area(self) -> None:
        # Arrange: explicitly offered room IDs and exact set area.
        data = native_proposal()
        info = cast(dict[str, object], data["objectInfo"])
        total = Decimal("22.50")
        for description, expected_area in (
            ("Продам комнаты № 2 и № 3 общая площадь 22,50 м².", total),
            (
                "Продаются комната № 2 и комната № 3 одним лотом, "
                "общей площадью 22,50 м².",
                total,
            ),
            (
                "Продам комнаты № 2, комната № 3 одним лотом. "
                "Общая площадь предлагаемых комнат 22,50 м².",
                total,
            ),
            (
                "Продам комнату № 2 площадью 9,2 м² и комнату № 3 "
                "площадью 10 м² одним лотом.",
                None,
            ),
            (
                "Продам комнату № 2 площадью 9,2 м² и комнату № 3 "
                "площадью 10 м² одним лотом, общей площадью 22,50 м².",
                total,
            ),
            (
                "Продам две комнаты: № 2 площадью 9,2 м² и № 3 площадью 10 м² "
                "одним лотом, общей площадью 22,50 м².",
                total,
            ),
        ):
            with self.subTest(description=description):
                # Arrange
                info["description"] = description

                # Act
                with self.assertRaises(RejectedCard) as rejected:
                    read_native(ROOM, data)
                accepted = read_native(ROOM, {**data, "roomsOffered": None})

                # Assert
                self.assertEqual(str(rejected.exception), "conflicting_room_count")
                assert accepted["state"] == "active"
                self.assertEqual(accepted["rooms"], 2)
                self.assertEqual(accepted["area_m2"], expected_area)

        for claim in (
            "Общая площадь предлагаемых комнат 23 м².",
            "Общая площадь этих двух комнат 23 м².",
            "Общая площадь обеих комнат 23 м².",
        ):
            with self.subTest(claim=claim):
                # Arrange: both published totals describe the same offered set.
                info["description"] = (
                    "Продам две комнаты общей площадью 22,50 м². " + claim
                )

                # Act
                with self.assertRaises(RejectedCard) as conflicting:
                    read_native(ROOM, {**data, "roomsOffered": 2})

                # Assert
                self.assertEqual(str(conflicting.exception), "conflicting_room_area")

        # Arrange: only one offer matches the card. A numbered partial
        # enumeration, the measured neighbor and a different group cannot
        # override the confirmed count or total.
        info["description"] = (
            "Продам три комнаты: № 2, общей площадью 32,50 м². "
            "Для сравнения, продам 1 комнату 9 м² в соседней квартире. "
            "Общая площадь обеих комнат 23 м² в соседней квартире."
        )

        # Act
        accepted = read_native(ROOM, {**data, "roomsOffered": 3})

        # Assert
        assert accepted["state"] == "active"
        self.assertEqual(
            (accepted["rooms"], accepted["area_m2"]), (3, Decimal("32.50"))
        )

        address = cast(dict[str, object], data["address"])
        address["displayName"] = "Самара, Солнечная улица, 7, кв 25, комната 2"
        for denial in (", но комната не выделена.", ". Комната не выделена."):
            with self.subTest(denial=denial):
                # Arrange: a positive allocation followed by a contradictory denial.
                info["description"] = "Доля соответствует комнате № 2" + denial

                # Act
                with self.assertRaises(RejectedCard) as unresolved:
                    read_native(ROOM, data)

                # Assert
                self.assertEqual(str(unresolved.exception), "unconfirmed_room_unit")

    def test_native_room_set_total_is_independent_of_the_room_count(self) -> None:
        # Arrange: the projection has a member area but no count for the offer.
        data = {**native_proposal(), "roomsOffered": None}
        info = cast(dict[str, object], data["objectInfo"])
        offer = "Продаются комнаты общей площадью 22,5 м² одним лотом."
        linked = "Общая площадь этих комнат — 22,50 м²."
        total = Decimal("22.50")
        for description, expected in (
            (offer, (None, total)),
            (f"{offer} {linked}", (None, total)),
            (f"{linked} Продам комнаты.", (None, total)),
            (f"{offer} Продам две комнаты.", (2, total)),
            ("Продаётся единый лот из комнат общей площадью 22,5 м².", (None, total)),
            (
                f"{offer} Общая площадь этих комнат 23 м² в соседней квартире.",
                (None, total),
            ),
            ("Продаются комнаты одним лотом.", (None, None)),
            (linked, (None, Decimal("9.20"))),
            (
                "Продаются комнаты общей площадью 23 м² по отдельности.",
                (None, Decimal("9.20")),
            ),
            (
                "Продаются комнаты общей площадью 23 м² одним лотом по соседству.",
                (None, Decimal("9.20")),
            ),
        ):
            with self.subTest(description=description):
                # Arrange
                info["description"] = description

                # Act
                snapshot = read_native(ROOM, data)

                # Assert
                assert snapshot["state"] == "active"
                self.assertEqual((snapshot["rooms"], snapshot["area_m2"]), expected)

    def test_native_room_set_keeps_totals_in_their_recognized_offer_scope(self) -> None:
        # Arrange: the own offer and a second, explicitly unrelated group.
        data = {**native_proposal(), "roomsOffered": None}
        info = cast(dict[str, object], data["objectInfo"])
        own = "Продаются комнаты общей площадью 22,5 м² одним лотом."
        linked = "Общая площадь этих комнат — 23 м²."
        for detached in (
            "Продаются комнаты в соседней квартире.",
            "Сдаются комнаты по отдельности.",
        ):
            for description in (
                f"{own} {detached} {linked}",
                f"{detached} {linked} {own}",
            ):
                with self.subTest(description=description):
                    # Arrange
                    info["description"] = description

                    # Act
                    snapshot = read_native(ROOM, data)

                    # Assert
                    assert snapshot["state"] == "active"
                    self.assertEqual(
                        (snapshot["rooms"], snapshot["area_m2"]),
                        (None, Decimal("22.50")),
                    )

            with self.subTest(detached=detached, own_total_conflicts=True):
                # Arrange: the attached total follows the own offer.
                info["description"] = f"{detached} {own} {linked}"

                # Act / Assert
                with self.assertRaisesRegex(RejectedCard, "^conflicting_room_area$"):
                    read_native(ROOM, data)

        # Arrange: two genuine offers make a generic reference ambiguous.
        info["description"] = (
            f"{own} Продаются комнаты в соседней квартире. "
            "Общая площадь предлагаемых комнат — 23 м²."
        )

        # Act
        snapshot = read_native(ROOM, data)

        # Assert
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["area_m2"], Decimal("22.50"))

    def test_native_room_set_rejects_conflicting_totals_before_count_is_known(
        self,
    ) -> None:
        # Arrange: both totals describe the one directly offered set.
        data = {**native_proposal(), "roomsOffered": None}
        info = cast(dict[str, object], data["objectInfo"])
        info["roomArea"] = None
        offer = "Продаются комнаты общей площадью 22,5 м² одним лотом."
        linked = "Общая площадь этих комнат — 23 м²."
        counted = "Продам две комнаты общей площадью 23 м²."
        for description in (
            f"{offer} {linked}",
            f"{linked} {offer}",
            f"{offer} {counted}",
            f"{counted} {offer}",
        ):
            with self.subTest(description=description):
                # Arrange
                info["description"] = description

                # Act / Assert
                with self.assertRaisesRegex(RejectedCard, "^conflicting_room_area$"):
                    read_native(ROOM, data)

        # Arrange: an explicit set cannot be projected as a single room.
        info["description"] = offer

        # Act / Assert
        with self.assertRaisesRegex(RejectedCard, "^conflicting_room_count$"):
            read_native(ROOM, {**data, "roomsOffered": 1})

    def test_room_member_identity_and_scope_precede_set_count(self) -> None:
        # Arrange: the structured projection identifies one offered room.
        data = native_proposal()
        info = cast(dict[str, object], data["objectInfo"])
        for description in (
            "Продам комнату № 2 площадью 9,2 м² и комнату № 3 "
            "площадью 10 м² по отдельности.",
            "Продам комнату № 2 площадью 9,2 м² и комнату № 2 площадью 9,2 м².",
        ):
            with self.subTest(description=description):
                # Arrange
                info["description"] = description

                # Act
                accepted = read_native(ROOM, data)

                # Assert
                assert accepted["state"] == "active"
                self.assertEqual(
                    (accepted["rooms"], accepted["area_m2"]), (1, Decimal("9.20"))
                )

        for first, second in (("9,2", "10"), ("10", "9,2")):
            with self.subTest(first=first, second=second):
                # Arrange: two measurements of the same explicitly named member.
                info["description"] = (
                    f"Продам комнату № 2 площадью {first} м² "
                    f"и комнату № 2 площадью {second} м²."
                )

                # Act
                with self.assertRaises(RejectedCard) as conflicting:
                    read_native(ROOM, data)

                # Assert
                self.assertEqual(str(conflicting.exception), "conflicting_room_area")

    def test_comparable_room_measurements_keep_the_identified_member_scope(
        self,
    ) -> None:
        # Arrange
        data = native_proposal()
        info = cast(dict[str, object], data["objectInfo"])
        member = "Продам комнату № 2 площадью 9,2 м²"
        offer_and_count = (
            (member + ".", 1),
            (member + " и комнату № 3 площадью 10 м² одним лотом.", 2),
        )
        for offer, count in offer_and_count:
            for claims in (
                (offer, "Площадь предлагаемой комнаты № 2 — 11 м²."),
                ("Площадь предлагаемой комнаты № 2 — 11 м².", offer),
            ):
                with self.subTest(count=count, claims=claims):
                    # Arrange
                    info["description"] = " ".join(claims)

                    # Act
                    with self.assertRaises(RejectedCard) as conflicting:
                        read_native(ROOM, {**data, "roomsOffered": count})

                    # Assert
                    self.assertEqual(
                        str(conflicting.exception), "conflicting_room_area"
                    )

        for claim in (
            "Площадь предлагаемой комнаты № 2 — 9,2 м².",
            "Площадь соседней комнаты № 2 — 11 м².",
            "Площадь комнаты № 2 — 11 м² в соседней квартире.",
        ):
            with self.subTest(claim=claim):
                # Arrange
                info["description"] = offer_and_count[1][0] + " " + claim

                # Act
                accepted = read_native(ROOM, {**data, "roomsOffered": 2})

                # Assert
                assert accepted["state"] == "active"
                self.assertEqual((accepted["rooms"], accepted["area_m2"]), (2, None))

    def test_measures_follow_the_offer_before_a_reused_room_number(self) -> None:
        own = "Продам комнату № 2 площадью 9,2 м²."
        foreign_claim = "Площадь этой комнаты № 2 — 11 м²."
        for foreign_offer in (
            "Продам комнату № 2 в соседней квартире.",
            "В соседней квартире: продаётся комната № 2.",
            "В соседней квартире\nПродаётся комната № 2.",
        ):
            for claims in (
                (own, foreign_offer, foreign_claim),
                (foreign_offer, foreign_claim, own),
            ):
                for structured in (None, 9.2):
                    with self.subTest(claims=claims, structured=structured):
                        # Arrange
                        data = native_proposal()
                        info = cast(dict[str, object], data["objectInfo"])
                        info.update(roomArea=structured, description=" ".join(claims))

                        # Act
                        snapshot = read_native(ROOM, data)

                        # Assert
                        assert snapshot["state"] == "active"
                        self.assertEqual(snapshot["rooms"], 1)
                        expected = Decimal("9.20") if structured is not None else None
                        self.assertEqual(snapshot["area_m2"], expected)

        # Arrange: the other offer's statement stays foreign; the owner's
        # explicitly re-established statement is compared with their own area.
        info["description"] = (
            f"{own} В соседней квартире: продам комнату № 2. {foreign_claim} "
            "В этой квартире: площадь этой комнаты № 2 — 10 м²."
        )

        # Act
        with self.assertRaises(RejectedCard) as conflict:
            read_native(ROOM, data)

        # Assert
        self.assertEqual(str(conflict.exception), "conflicting_room_area")

    def test_foreign_heading_can_neither_supply_nor_override_a_set_total(self) -> None:
        for own_offer, area in (
            ("Продам комнаты.", None),
            ("Продам комнаты общей площадью 22,5 м².", Decimal("22.50")),
        ):
            for heading in ("В соседней квартире:", "В соседней квартире\n"):
                foreign_offer = (
                    f"{heading} Продаются три комнаты. "
                    "Общая площадь этих комнат — 23 м²."
                )
                for claims in ((own_offer, foreign_offer), (foreign_offer, own_offer)):
                    with self.subTest(claims=claims):
                        # Arrange
                        data = {**native_proposal(), "roomsOffered": None}
                        info = cast(dict[str, object], data["objectInfo"])
                        info["description"] = " ".join(claims)

                        # Act
                        snapshot = read_native(ROOM, data)

                        # Assert
                        assert snapshot["state"] == "active"
                        self.assertEqual(
                            (snapshot["rooms"], snapshot["area_m2"]), (None, area)
                        )

    def test_foreign_measurement_and_competing_offer_have_distinct_scope(self) -> None:
        own = (
            "Продам комнату № 2 площадью 9,2 м² и комнату № 3 "
            "площадью 10 м² одним лотом. Общая площадь этих комнат — 22,5 м²."
        )
        competitor = "В соседней квартире: сдаются комнаты № 2 и № 3."
        for foreign, direct, relative, equal, conflict in (
            (
                "Площадь этой комнаты № 2 — 7 м² в соседней квартире.",
                "Площадь предлагаемой комнаты № 2",
                "Площадь этой комнаты № 2",
                "9,2",
                "11",
            ),
            (
                "Общая площадь этих комнат — 20 м² в соседней квартире.",
                "Общая площадь предлагаемых комнат",
                "Общая площадь этих комнат",
                "22,5",
                "23",
            ),
        ):
            for context, label, amount, rejected in (
                (foreign, direct, equal, False),
                (foreign, direct, conflict, True),
                (foreign, relative, conflict, False),
                (competitor, direct, conflict, False),
                (competitor, "В этой квартире: " + direct, conflict, True),
                (
                    competitor + " В этой квартире: хороший ремонт.",
                    direct,
                    conflict,
                    True,
                ),
            ):
                with self.subTest(label=label, context=context, amount=amount):
                    # Arrange
                    data = {**native_proposal(), "roomsOffered": 2}
                    info = cast(dict[str, object], data["objectInfo"])
                    info["description"] = f"{own} {context} {label} — {amount} м²."

                    if rejected:
                        # Act
                        with self.assertRaises(RejectedCard) as error:
                            read_native(ROOM, data)

                        # Assert
                        self.assertEqual(str(error.exception), "conflicting_room_area")
                    else:
                        # Act
                        snapshot = read_native(ROOM, data)

                        # Assert
                        assert snapshot["state"] == "active"
                        self.assertEqual(snapshot["area_m2"], Decimal("22.50"))

    def test_native_house_rejects_incompatible_direct_offer_claims(self) -> None:
        # Arrange: contrast directly offered properties with incidental mentions.
        data = native_proposal()
        data.update(
            {
                "id": 2079647938,
                "offerType": "house",
                "land": {"area": "6,24", "areaUnit": "сотка"},
            }
        )
        cases = (
            ("Продаются два дома на участке.", "unsupported_house_unit"),
            ("Продаётся единый лот из двух домов.", "unsupported_house_unit"),
            (
                "Продаю дом на участке и второй дом в том же лоте.",
                "unsupported_house_unit",
            ),
            (
                "Продаётся дом площадью 100 м² и самостоятельный блок № 2 "
                "площадью 40 м² одним лотом.",
                "unsupported_house_unit",
            ),
            (
                "Продаётся жилой блок площадью 55 м² и дом "
                "площадью 100 м² одним лотом.",
                "unsupported_house_unit",
            ),
            (
                "Продаётся дом площадью 100 м², дом площадью 80 м² "
                "и жилой блок площадью 55 м² одним лотом.",
                "unsupported_house_unit",
            ),
            (
                "Продаётся дом площадью 100 м² и второй дом "
                "площадью 80 м² по отдельности.",
                None,
            ),
            (
                "Продаётся дом площадью 100 м² и самостоятельный блок № 2 "
                "площадью 40 м² отдельно.",
                None,
            ),
            ("Продаю дом № 8.", "conflicting_house_unit"),
            ("Площадь участка 7 соток.", "conflicting_land_area"),
            (
                "Продаётся дом общей площадью 100 м² на участке 5 соток.",
                "conflicting_land_area",
            ),
            ("Продаётся дом 100 м² на участке 6,24 сотки.", None),
            (
                "Продаётся дом 100 м²; соседний дом на участке 5 соток.",
                None,
            ),
            ("Предлагается земля. Продам только блок.", "unconfirmed_house_block"),
            ("Продаётся дом, точнее блок № 2.", "unconfirmed_house_block"),
            ("Продаётся дом, точнее — блок № 2.", "unconfirmed_house_block"),
            ("Продаётся дом 100 м², точнее блок № 2.", "unconfirmed_house_block"),
            ("Срочно. Продам только земельный участок.", "unsupported_house_unit"),
            ("Продам типовой проект дома.", "unsupported_house_unit"),
            ("Для семьи. Продаётся половина дома.", "unconfirmed_house_block"),
            ("Есть сад. Продаётся невыделенная доля дома.", "unallocated_house_share"),
            ("Неделимая доля в доме.", "unallocated_house_share"),
            (
                "Хороший район. Продаю дом общей площадью 120 м².",
                "conflicting_house_area",
            ),
            ("Рядом стоят два дома. Площадь гаража 30 м².", None),
        )
        for description, problem in cases:
            with self.subTest(description=description):
                data["objectInfo"] = {"area": 100, "description": description}
                if problem is None:
                    # Act
                    accepted = read_native(HOUSE, data)

                    # Assert
                    assert accepted["state"] == "active"
                    self.assertEqual(accepted["area_m2"], Decimal("100.00"))
                else:
                    # Act
                    with self.assertRaises(RejectedCard) as rejected:
                        read_native(HOUSE, data)

                    # Assert
                    self.assertEqual(str(rejected.exception), problem)

        address = cast(dict[str, object], data["address"])
        for displayed in (
            "Самара, Солнечная улица, 7, блок",
            "Самара, Солнечная улица, дом 7 — блок",
            "Самара, Солнечная улица, дом 7, блок 2, дом 8, блок 3",
        ):
            with self.subTest(displayed=displayed):
                # Arrange: unaddressed or competing block markers.
                address["displayName"] = displayed
                data["objectInfo"] = {"area": 100}

                # Act
                with self.assertRaises(RejectedCard) as unresolved:
                    read_native(HOUSE, data)

                # Assert
                self.assertEqual(str(unresolved.exception), "unconfirmed_house_block")

    def test_home_converts_land_exactly_and_keeps_partial_address(self) -> None:
        # Arrange
        data = native_proposal()
        data.update(
            {
                "id": 2079647938,
                "offerType": "house",
                "address": {
                    "displayName": "Россия, Самара, Северный переулок",
                    "parents": [
                        {"kind": "locality", "name": "СНТ Яблонька"},
                        {"kind": "locality", "name": "Самара"},
                        {
                            "kind": "province",
                            "subkind": "region",
                            "name": "Самарская область",
                        },
                    ],
                },
                "objectInfo": {"area": 100, "rooms": None, "floor": None},
                "house": {"info": {"floors": 1}},
                "land": {"area": "6,24", "areaUnit": "сотка"},
            }
        )

        # Act
        result = read_native(HOUSE, data)

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["area_m2"], Decimal("100.00"))
        self.assertEqual(result["land_area_m2"], Decimal("624.00"))
        self.assertIsNone(result["floor"])
        self.assertEqual(
            result["address"],
            "Самарская область, Самара, СНТ Яблонька, Северный переулок",
        )

    def test_house_subtypes_require_the_address_of_one_independent_unit(self) -> None:
        # Arrange: selected fields from the four native subtype observations.
        cases = (
            ("sale", "house_part", 1525510020, "Тольятти", "Рабочий проезд", None, 69),
            (
                "rent",
                "house_part",
                2081525526,
                "Тольятти",
                "КП Чайка, 23а",
                "Половина большого бревенчатого дома",
                250,
            ),
            (
                "sale",
                "townhouse",
                1695973117,
                "Самара",
                "Сарапульский переулок, 38",
                "Продается здание общежития. Также возможна продажа по комнатам.",
                394.1,
            ),
            (
                "rent",
                "townhouse",
                1885120006,
                "Чапаевск",
                "Херсонская улица, 7/2",
                "Сдается дом",
                140,
            ),
        )
        for deal, kind, identifier, locality, address, description, area in cases:
            with self.subTest(identifier=identifier):
                data = native_proposal()
                data.update(
                    {
                        "id": identifier,
                        "offerType": kind,
                        "dealType": deal,
                        "address": {
                            "displayName": f"Россия, Самарская область, {locality}, {address}",
                            "name": address,
                            "parents": [
                                {"kind": "locality", "name": locality},
                                {
                                    "kind": "province",
                                    "subkind": "region",
                                    "name": "Самарская область",
                                },
                            ],
                        },
                        "objectInfo": {"area": area, "description": description},
                    }
                )
                url = f"https://samara.domclick.ru/card/{deal}__{kind}__{identifier}"

                if deal == "rent" and kind == "townhouse":
                    # Act
                    result = read_native(url, data)

                    # Assert
                    assert result["state"] == "active"
                    self.assertEqual(result["property_type_id"], 3)
                    self.assertEqual(result["deal_type"], "month_rent")
                    self.assertEqual(result["area_m2"], Decimal("140.00"))
                    self.assertEqual(
                        result["address"],
                        "Самарская область, Чапаевск, Херсонская улица, 7/2",
                    )
                else:
                    # Act
                    with self.assertRaises(RejectedCard) as rejected:
                        read_native(url, data)

                    # Assert
                    self.assertEqual(
                        str(rejected.exception),
                        "unsupported_house_unit"
                        if kind == "townhouse"
                        else "unconfirmed_house_block",
                    )

    def test_townhouse_cannot_rely_on_a_slash_in_display_address(self) -> None:
        # Arrange
        url = "https://samara.domclick.ru/card/rent__townhouse__1885120006"
        data = native_proposal()
        data.update(id=1885120006, offerType="townhouse", dealType="rent")
        address = cast(dict[str, object], data["address"])
        address["displayName"] = "Чапаевск, Херсонская улица, 7/2"

        # Act
        with self.assertRaises(RejectedCard) as rejected:
            read_native(url, data)

        # Assert
        self.assertEqual(str(rejected.exception), "unconfirmed_house_block")

    def test_addressable_duplex_with_conflicting_published_area_is_rejected(
        self,
    ) -> None:
        # Arrange: the comparable fields in the 2080868633 card observation.
        url = "https://samara.domclick.ru/card/sale__townhouse__2080868633"
        data = native_proposal()
        data.update(
            {
                "id": 2080868633,
                "offerType": "townhouse",
                "address": {
                    "displayName": "Самарская область, Янтарная улица, 5/2",
                    "parents": [
                        {
                            "kind": "locality",
                            "name": "коттеджный посёлок Union Village",
                        },
                        {
                            "kind": "province",
                            "subkind": "region",
                            "name": "Самарская область",
                        },
                    ],
                },
                "objectInfo": {
                    "area": 105.3,
                    "description": "Одноэтажный дуплекс 100 м2.",
                },
            }
        )

        # Act
        with self.assertRaises(RejectedCard) as rejected:
            read_native(url, data)

        # Assert
        self.assertEqual(str(rejected.exception), "conflicting_house_area")

    def test_zero_rooms_requires_an_explicit_flat_studio_title(self) -> None:
        # Arrange
        url = "https://samara.domclick.ru/card/sale__flat__2080269220"
        data = native_proposal()
        data.update({"id": 2080269220, "offerType": "flat", "objectInfo": {"rooms": 0}})
        script = ssr({"productCard": data})

        # Act
        result = parse_card(
            url,
            OBSERVED,
            [script],
            [],
            loaded_url=url,
            page_title="Купить квартиру-студию, 27 м² по адресу Самара",
        )
        with self.assertRaises(RejectedCard) as rejected:
            parse_card(
                url,
                OBSERVED,
                [script],
                [],
                loaded_url=url,
                page_title="Студия дизайна рядом",
            )

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["rooms"], 0)
        self.assertEqual(str(rejected.exception), "unconfirmed_studio")

    def test_wrong_unit_and_nonrepresentable_area_stop_before_p3(self) -> None:
        for land in (
            {"area": 4, "areaUnit": "акр"},
            {"area": "1.00001", "areaUnit": "сотка"},
        ):
            with self.subTest(land=land):
                # Arrange
                data = native_proposal()
                data.update({"id": 2079647938, "offerType": "house", "land": land})

                # Act
                with self.assertRaises(RejectedCard) as rejected:
                    read_native(HOUSE, data)

                # Assert
                self.assertIn(
                    str(rejected.exception), ("invalid_land_unit", "invalid_precision")
                )

    def test_valid_key_has_exact_host_and_path(self) -> None:
        # Arrange
        positive = {
            "https://DOMCLICK.SUTOCHNO.RU:443/detail/2252883/?guests=3#photo": "/detail/2252883",
            DAILY.upper()
            .replace("HTTPS", "https")
            .replace("/CARD/", "/card/"): card_key(DAILY),
            ROOM + "/?foo=1": "/card/sale__room__2071752356",
        }
        negative = (
            "https://domclick.sutochno.ru.example.org/detail/2252883",
            "https://domclick.sutochno.ru/detail/02252883",
            "https://domclick.sutochno.ru:444/detail/2252883",
            "https://user@domclick.sutochno.ru/detail/2252883",
            "https://domclick.sutochno.ru/detail/%32252883",
            "https://domclick.sutochno.ru/detail/2252883/other",
            "https://unknown.domclick.ru/card/sale__room__2071752356",
            "https://24.domclick.ru/card/sale__room__2071752356",
        )

        # Act
        supported = {candidate: card_key(candidate) for candidate in positive}
        refused = [card_key(candidate) for candidate in negative]

        # Assert
        self.assertEqual(supported, positive)
        self.assertEqual(refused, [None] * len(negative))

    def test_foreign_publication_does_not_replace_a_known_key(self) -> None:
        # Arrange
        data = native_proposal()
        data["id"] = 2071752357
        script = ssr({"productCard": data})

        # Act
        mismatch = parse_card(ROOM, OBSERVED, [script], [], loaded_url=ROOM)
        redirect = parse_card(ROOM, OBSERVED, [script], [], loaded_url=DAILY)

        # Assert
        expected = {
            "source_code": "domclick",
            "external_id": "/card/sale__room__2071752356",
            "observed_at": OBSERVED,
            "state": "unavailable",
        }
        self.assertEqual(mismatch, expected)
        self.assertEqual(redirect, expected)

    def test_removed_requires_explicit_marker_and_rejects_contradictory_identity(
        self,
    ) -> None:
        # Arrange
        data = native_proposal()
        data.update({"id": 2079647938, "offerType": "house", "status": 8})

        # Act
        bare = read_native(HOUSE, data)
        removed = read_native(HOUSE, data, removed=True)
        conflict = read_native(ROOM, data, removed=True)
        empty = read_native(HOUSE, {}, removed=True)
        missing_id = read_native(HOUSE, {**data, "id": None}, removed=True)
        still_active = read_native(HOUSE, {**data, "status": 2}, removed=True)

        # Assert
        self.assertEqual(bare["state"], "unavailable")
        self.assertEqual(removed["state"], "removed")
        self.assertEqual(conflict["state"], "unavailable")
        self.assertEqual(empty["state"], "unavailable")
        self.assertEqual(missing_id["state"], "unavailable")
        self.assertEqual(still_active["state"], "active")

    def test_native_removal_requires_the_own_card_and_main_marker(self) -> None:
        # Arrange: observed native ID, status and text with controlled placement.
        exact = "<main><h1>Объявление снято с размещения</h1></main>"
        notice = "<main><aside><h1>Объявление снято с размещения</h1></aside></main>"
        card = {"id": 2079647938, "status": 8, "offerType": "house", "dealType": "sale"}
        cases: tuple[tuple[str, dict[str, object], str], ...] = (
            (exact, card, "removed"),
            (notice, card, "unavailable"),
            (exact, {}, "unavailable"),
            (exact, {**card, "id": 2079647939}, "unavailable"),
        )

        def serve(route: Route, *, body: str) -> None:
            route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=body
            )

        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium", headless=True) as browser,
            browser.new_context() as context,
        ):
            for markup, state, expected in cases:
                with self.subTest(expected=expected, state=state):
                    # Act
                    page = context.new_page()
                    body = f"{markup}<script>{ssr({'productCard': state})}</script>"
                    page.route(HOUSE, partial(serve, body=body))
                    page.goto(HOUSE)
                    result = read_card(page, HOUSE, OBSERVED)
                    page.close()

                    # Assert
                    self.assertEqual(result["state"], expected)

    def test_native_new_flat_reads_only_published_unit_fields(self) -> None:
        # Arrange: numeric fields from the card observation and published JS
        # selectors. The address-region binding is controlled, not a live copy.
        url = "https://samara.domclick.ru/card/sale__new_flat__2072470551"
        data = {
            "id": 2072470551,
            "address": {
                "guid": "controlled-address-key",
                "shortDisplayName": "Гвардейский переулок, 7а",
                "locality": {"name": "Самара"},
                "position": {"lat": 53.22549, "lon": 50.23738},
            },
            "objectInfo": {"area": 54.3, "rooms": 2, "floorNumber": 2},
            "building": {"floors": 17},
            "flatInfo": {"flatNumber": "121"},
            "priceInfo": {"currentPrice": 8304840},
            "media": {"photos": ["/vitrina/8b/4a/file.webp"]},
            "complex": {"description": "Общий текст ЖК"},
        }
        regions = {"guid": "controlled-address-key", "regionName": "Самарская область"}
        shell = {"isDeletedFlat": False, "flatDetails": {"data": data}}

        # Act
        result = parse_card(
            url,
            OBSERVED,
            [ssr({"shell": shell, "regions": regions})],
            [],
            loaded_url=url,
        )

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["market_segment"], "new_build")
        self.assertEqual(result["region_id"], 68)
        self.assertEqual(result["area_m2"], Decimal("54.30"))
        self.assertEqual(result["floor"], 2)
        self.assertEqual(result["floors_total"], 17)
        self.assertEqual(result["address"], "Самара, Гвардейский переулок, 7а, кв. 121")
        self.assertEqual(
            result["price"],
            {
                "status": "known",
                "price_rub": Decimal("8304840.00"),
                "price_kind": "listed",
                "currency": "RUB",
            },
        )
        self.assertIsNone(result["description"])
        self.assertEqual(result["photos"], {"status": "unavailable"})

        # Arrange: the default region belongs to a different address.
        regions["guid"] = "unrelated-address-key"

        # Act
        with self.assertRaises(RejectedCard) as rejected:
            parse_card(
                url,
                OBSERVED,
                [ssr({"shell": shell, "regions": regions})],
                [],
                loaded_url=url,
            )

        # Assert
        self.assertEqual(str(rejected.exception), "invalid_region")

    def test_partner_decodes_refs_and_returns_the_confirmed_flat(self) -> None:
        # Arrange
        script = nuxt_script(partner_projection())

        # Act
        result = parse_card(
            PARTNER, OBSERVED, [], [script], loaded_url=PARTNER + "?price_per=1"
        )

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["external_id"], "/detail/2252883")
        self.assertEqual(result["source_code"], "domclick")
        self.assertEqual(result["deal_type"], "day_rent")
        self.assertEqual(result["latitude"], Decimal("53.1085300"))
        self.assertEqual(result["price"], {"status": "unavailable"})
        self.assertEqual(result["description"], "Описание продавца.\n23 м².")
        self.assertEqual(result["photos"]["status"], "known")

    def test_partner_room_with_hotel_parent_never_makes_p3(self) -> None:
        # Arrange: deliberate unit counterexample using the observed Nuxt shape.
        detail = partner_projection()
        detail.update({"type": {"name": "room"}, "hotel": {"id": 1, "title": "Отель"}})
        script = nuxt_script(detail)

        # Act
        with self.assertRaises(RejectedCard) as rejected:
            parse_card(PARTNER, OBSERVED, [], [script], loaded_url=PARTNER)

        # Assert
        self.assertEqual(str(rejected.exception), "hotel_or_ambiguous_room")

    def test_daily_read_uses_same_link_visible_one_night_quote_and_main_gallery(
        self,
    ) -> None:
        # Arrange: replay selected live DOM controls and publication-shaped state.
        expected_uuid = DAILY.rsplit("/", 1)[1]
        content, result_section = daily_listing_parts()
        recommendation = result_section.replace(
            '<section class="QrLw3">', '<section aria-label="Рекомендации">'
        ).replace("3 910", "1 900")

        def layout(body: str) -> str:
            return f'{content}<div id="app"><div>{body}</div></div>'

        dom = layout(result_section + recommendation)
        card = (
            f"<script>{daily_ssr(daily_projection())}delete window.__SSR_STATE__;</script>"
            '<div data-e2e-id="gallery-widget">'
            f'<img src="{DAILY_PHOTO}"></div>'
        )

        def serve(route: Route) -> None:
            if route.request.url in (LISTING, DAILY):
                route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=dom if route.request.url == LISTING else card,
                )
            else:
                route.abort()

        # Act
        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium", headless=True) as browser,
            browser.new_context(service_workers="block") as context,
        ):
            context.route("**/*", serve)
            listing = context.new_page()
            listing.goto(LISTING)
            quote = read_daily_listing_quote(listing, DAILY)
            page = context.new_page()
            page.goto(DAILY)
            snapshot = read_card(page, DAILY, OBSERVED, quote=quote)

            # Repeat with an explicitly labelled recommendation, a duplicate
            # result root, and a row whose position disagrees with the SSR ID.
            uncertain = []
            for candidate in (
                recommendation,
                result_section.replace(
                    "<section ", '<section aria-label="Рекомендации" '
                ),
                result_section + result_section,
                result_section.replace('data-index="0"', 'data-index="1"'),
            ):
                dom = layout(candidate)
                listing.goto(LISTING)
                uncertain.append(read_daily_listing_quote(listing, DAILY))
            for published in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
                dom = layout(result_section).replace(expected_uuid.upper(), published)
                listing.goto(LISTING)
                uncertain.append(read_daily_listing_quote(listing, DAILY))

        # Assert
        self.assertEqual(uncertain, [None] * 6)
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["external_id"], card_key(DAILY))
        self.assertEqual(snapshot["area_m2"], Decimal("36.00"))
        self.assertEqual(
            snapshot["price"],
            {
                "status": "known",
                "price_rub": Decimal("3910.00"),
                "price_kind": "listed",
                "currency": "RUB",
            },
        )
        self.assertEqual(snapshot["photos"], {"status": "known", "urls": [DAILY_PHOTO]})

    def test_daily_wrong_context_has_no_price_and_incomplete_photos_stay_unavailable(
        self,
    ) -> None:
        for quote in (
            None,
            DailyListingQuote(
                DAILY, "1–2 мая", "1 гость", "3 910 ₽", "3 910 ₽ за сутки"
            ),
            DailyListingQuote(DAILY, "", "2 гостя", "3 910 ₽", "3 910 ₽ за сутки"),
            DailyListingQuote(DAILY, "", "1 гость", "3 910 ₽", "3 910 ₽ за неделю"),
            DailyListingQuote(DAILY, "", "1 гость", "3 910 ₽", "2 000 ₽ за сутки"),
        ):
            with self.subTest(quote=quote):
                # Arrange
                script = daily_ssr(daily_projection())

                # Act
                result = parse_card(
                    DAILY,
                    OBSERVED,
                    [script],
                    [],
                    loaded_url=DAILY + "?adt=1",
                    quote=quote,
                )

                # Assert
                assert result["state"] == "active"
                self.assertEqual(result["price"], {"status": "unavailable"})
                self.assertEqual(result["photos"], {"status": "unavailable"})

    def test_daily_public_id_is_never_taken_from_internal_uid(self) -> None:
        # Arrange
        data = daily_projection()
        card_uuid = DAILY.rsplit("/", 1)[1]
        data["uid"] = card_uuid
        script = daily_ssr(data).replace(
            f'"searchData": {{"id": "{card_uuid}"}}',
            '"searchData": {"id": "98497d9e-fe9c-44b1-8ec5-902e6d27763b"}',
        )

        # Act
        result = parse_card(DAILY, OBSERVED, [script], [], loaded_url=DAILY)

        # Assert
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["external_id"], card_key(DAILY))

    def test_daily_keeps_from_price_and_half_even_coordinate_rounding(self) -> None:
        # Arrange
        data = daily_projection()
        data["geoInfo"] = {
            "address": "Самарская область, пос. Придорожный, Николаевский проспект, 31",
            "lat": 53.11200925,
            "lon": 50.14511335,
        }
        quote = DailyListingQuote(
            DAILY, "", "1 гость", "от 2 300 ₽", "от 2 300 ₽ за сутки"
        )

        # Act
        result = parse_card(
            DAILY, OBSERVED, [daily_ssr(data)], [], loaded_url=DAILY, quote=quote
        )

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["latitude"], Decimal("53.1120092"))
        self.assertEqual(result["longitude"], Decimal("50.1451134"))
        self.assertEqual(
            result["price"],
            {
                "status": "known",
                "price_rub": Decimal("2300.00"),
                "price_kind": "from",
                "currency": "RUB",
            },
        )

    def test_house_part_keeps_the_explicit_block_scope_and_number(self) -> None:
        # Arrange: a control using the accepted 6.3 address notation.
        url = "https://samara.domclick.ru/card/sale__house_part__7777777"
        data = native_proposal()
        data.update(
            {
                "id": 7777777,
                "offerType": "house_part",
                "address": {
                    "displayName": "Самара, Солнечная улица, 7, блок 2",
                    "parents": [
                        {"kind": "locality", "name": "Самара"},
                        {
                            "kind": "province",
                            "subkind": "region",
                            "name": "Самарская область",
                        },
                    ],
                },
                "objectInfo": {"area": 55, "description": "Самостоятельная секция."},
            }
        )

        # Act
        result = read_native(url, data)

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["area_m2"], Decimal("55.00"))
        self.assertEqual(
            result["address"], "Самарская область, Самара, Солнечная улица, 7, блок 2"
        )

        # Arrange: the same address belongs to a block whose sold share is unallocated.
        data["objectInfo"] = {
            "area": 55,
            "description": "Продаётся 1/2 доля в блоке 2. Самостоятельная часть доли не выделена.",
        }

        # Act
        with self.assertRaises(RejectedCard) as rejected:
            read_native(url, data)

        # Assert
        self.assertEqual(str(rejected.exception), "unallocated_house_share")

        # Arrange: an underlying share remains ambiguous without the negation.
        data["objectInfo"] = {
            "area": 55,
            "description": "Продаётся 1/2 доля в блоке 2.",
        }

        # Act
        with self.assertRaises(RejectedCard) as rejected:
            read_native(url, data)

        # Assert
        self.assertEqual(str(rejected.exception), "unallocated_house_share")

    def test_block_area_is_not_the_enclosing_building_area(self) -> None:
        # Arrange: the explicit block is № 2 and its own structured area is 55 m².
        data = native_proposal()
        address = cast(dict[str, object], data["address"])
        address["displayName"] = "Самара, Солнечная улица, 7, блок 2"
        url = "https://samara.domclick.ru/card/sale__house_part__7777777"
        data.update(
            id=7777777,
            offerType="house_part",
            land={"area": "6,24", "areaUnit": "сотка"},
        )
        cases = (
            ("Продаётся блок № 3 площадью 55 м².", "conflicting_house_unit"),
            ("Продаётся дом, точнее блок № 3.", "conflicting_house_unit"),
            (
                "Продаётся блок № 2 площадью 55 м² на участке 5 соток.",
                "conflicting_land_area",
            ),
            (
                "Продаётся дом, точнее блок № 2 на участке 5 соток.",
                "conflicting_land_area",
            ),
            (
                "Продаётся дом, точнее блок № 2 на участке 6,24 сотки.",
                None,
            ),
            (
                "Продаётся блок № 2 площадью 55 м². Соседний дом на участке 5 соток.",
                None,
            ),
            (
                "Продаётся блок № 2 площадью 55 м². "
                "Площадь предлагаемого участка — 5 соток.",
                "conflicting_land_area",
            ),
            (
                "Площадь этого земельного участка: 6,24 сотки. "
                "Продаётся блок № 2 площадью 55 м².",
                None,
            ),
            (
                "Продаётся блок № 2 площадью 55 м². "
                "Площадь участка 5 соток в соседнем доме.",
                None,
            ),
            ("Площадь секции 60 м².", "conflicting_house_area"),
            ("Площадь секции 55 м². Общая площадь дома 100 м².", None),
        )
        for text, problem in cases:
            with self.subTest(text=text):
                data["objectInfo"] = {"area": 55, "description": text}
                if problem:
                    # Act
                    with self.assertRaises(RejectedCard) as rejected:
                        read_native(url, data)

                    # Assert
                    self.assertEqual(str(rejected.exception), problem)
                else:
                    # Act
                    accepted = read_native(url, data)

                    # Assert
                    assert accepted["state"] == "active"
                    self.assertEqual(accepted["area_m2"], Decimal("55.00"))

    def test_daily_studio_uses_published_unit_title_with_generic_page_title(
        self,
    ) -> None:
        # Arrange
        card = daily_projection()
        card.update({"rooms": 0, "title": "Квартира-студия"})

        # Act
        result = parse_card(
            DAILY,
            OBSERVED,
            [daily_ssr(card)],
            [],
            loaded_url=DAILY,
            page_title="Домклик24",
        )

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["rooms"], 0)

    def test_daily_studio_rejects_conflicting_public_unit_titles(self) -> None:
        for heading, reason in (
            ("Снять однокомнатную квартиру 36 м² посуточно", "contradictory_rooms"),
            ("Снять частный дом", "contradictory_daily_type"),
        ):
            with self.subTest(heading=heading):
                # Arrange
                card = daily_projection()
                card.update({"rooms": 0, "title": "Квартира-студия"})

                # Act
                with self.assertRaises(RejectedCard) as rejected:
                    parse_card(
                        DAILY,
                        OBSERVED,
                        [daily_ssr(card)],
                        [],
                        loaded_url=DAILY,
                        page_title=heading,
                    )

                # Assert
                self.assertEqual(str(rejected.exception), reason)
