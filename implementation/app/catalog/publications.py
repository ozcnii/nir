"""Accept and persist normalized publication snapshots."""

from collections.abc import Mapping
from datetime import datetime
from typing import cast

from django.db import IntegrityError
from django.db.models import Exists
from psycopg.errors import CheckViolation

from app.catalog.address_parser import parse_address
from app.catalog.contracts import (
    PROPERTY_FACT_FIELDS,
    ActiveSnapshot,
    Deal,
    KnownPrice,
    SnapshotOutcome,
    SnapshotResult,
    has_active_payload,
    is_nonblank_text,
    is_publication_url,
)
from app.catalog.locks import catalog_transaction
from app.catalog.matching import (
    Match,
    choose_flat,
    repeated_flat_patch,
)
from app.catalog.matching_queries import flat_candidates, saved_property
from app.catalog.models import (
    Ad,
    DealType,
    PriceHistory,
    Property,
    PropertyType,
    Region,
    Source,
)
from app.catalog.reference_defaults import APARTMENT_TYPE_ID, REGION_NAMES, SOURCE_CODES


def accept_snapshot(snapshot: Mapping[str, object]) -> SnapshotResult:
    """Accept one P3 snapshot atomically."""
    source = snapshot.get("source_code")
    external_id = snapshot.get("external_id")
    observed_at = snapshot.get("observed_at")
    state = snapshot.get("state")
    if (
        source not in SOURCE_CODES
        or not is_nonblank_text(external_id)
        or not isinstance(observed_at, datetime)
        or observed_at.utcoffset() is None
        or state not in ("active", "removed", "unavailable")
    ):
        return SnapshotResult("rejected", reason="invalid_envelope")
    if state == "unavailable":
        return SnapshotResult("unavailable")

    try:
        with catalog_transaction(wait=True):
            return _write_snapshot(
                cast(str, source), external_id, snapshot, observed_at
            )
    except IntegrityError as error:
        cause = error.__cause__
        if (
            isinstance(cause, CheckViolation)
            and cause.diag.constraint_name == "ad_photos_url_array"
        ):
            return SnapshotResult("rejected", reason="invalid_active_snapshot")
        raise


def _write_snapshot(
    source: str, external_id: str, snapshot: Mapping[str, object], observed_at: datetime
) -> SnapshotResult:
    publication = Ad.objects.filter(source_id=source, external_id=external_id)
    previous = publication.first()
    if previous is not None and observed_at <= previous.observed_at:
        return SnapshotResult(
            "ignored" if observed_at == previous.observed_at else "stale",
            previous.id,
            previous.property_id,
        )
    deal = snapshot.get("deal_type")
    if previous is not None and deal in DealType.values and deal != previous.deal_type:
        return SnapshotResult(
            "conflict",
            previous.id,
            previous.property_id,
            reason="deal_type_changed",
            previous_deal_type=cast(Deal, previous.deal_type),
            incoming_deal_type=cast(Deal, deal),
        )
    if snapshot["state"] == "removed":
        if deal not in DealType.values or not is_publication_url(snapshot.get("url")):
            return SnapshotResult("rejected", reason="invalid_removed_snapshot")
        if previous is None:
            return SnapshotResult("not_found")
        publication.update(is_active=False, observed_at=observed_at)
        return SnapshotResult(
            "updated" if previous.is_active else "unchanged",
            previous.id,
            previous.property_id,
        )
    if not has_active_payload(snapshot):
        return SnapshotResult("rejected", reason="invalid_active_snapshot")
    if not _references_exist(snapshot):
        return SnapshotResult("rejected", reason="unknown_reference")
    match: Match | None = None
    region = REGION_NAMES.get(snapshot["region_id"])
    if previous is not None:
        property_id = previous.property_id
        saved = saved_property(property_id)
        if APARTMENT_TYPE_ID in (
            saved["property_type_id"],
            snapshot["property_type_id"],
        ):
            if (
                saved["region_id"] != snapshot["region_id"]
                or saved["property_type_id"] != snapshot["property_type_id"]
            ):
                return SnapshotResult(
                    "conflict",
                    previous.id,
                    property_id,
                    reason="property_scope_changed",
                )
            if region is not None:
                patch = repeated_flat_patch(saved, snapshot, region)
                if patch is None:
                    return SnapshotResult(
                        "conflict",
                        previous.id,
                        property_id,
                        reason="apartment_address_changed",
                    )
                match = Match(property_id, patch)
    elif snapshot["property_type_id"] == APARTMENT_TYPE_ID and region is not None:
        address = parse_address(snapshot["address"], region)
        if address is not None:
            match = choose_flat(
                snapshot,
                address,
                flat_candidates(snapshot["region_id"]),
                region,
            )
    return _store_active(snapshot, previous, match)


def _references_exist(data: ActiveSnapshot) -> bool:
    return (
        Region.objects.filter(pk=data["region_id"])
        .filter(
            Exists(PropertyType.objects.filter(pk=data["property_type_id"])),
            Exists(Source.objects.filter(pk=data["source_code"])),
        )
        .exists()
    )


def _store_active(
    data: ActiveSnapshot, previous: Ad | None, match: Match | None
) -> SnapshotResult:
    fields = {
        "url": data["url"],
        "market_segment": data["market_segment"],
        "description": data["description"],
        "is_active": True,
    }
    updates: dict[str, object] = {**fields, "observed_at": data["observed_at"]}
    photo_state = data["photos"]
    photos = photo_state["urls"] if photo_state["status"] == "known" else None
    if photos is not None:
        updates["photos"] = photos
        updates["photos_observed_at"] = data["observed_at"]

    if match is not None and match.patch:
        Property.objects.filter(pk=match.property_id).update(**match.patch)

    if previous is None:
        property_id = (
            match.property_id
            if match is not None
            else Property.objects.create(
                region_id=data["region_id"],
                property_type_id=data["property_type_id"],
                **{key: data.get(key) for key in PROPERTY_FACT_FIELDS},
            ).id
        )
        ad_id = Ad.objects.create(
            property_id=property_id,
            source_id=data["source_code"],
            external_id=data["external_id"],
            deal_type=data["deal_type"],
            **updates,
        ).id
        changed = False
    else:
        ad_id, property_id = previous.id, previous.property_id
        changed = any(getattr(previous, key) != value for key, value in fields.items())
        changed |= photos is not None and photos != previous.photos
        changed |= match is not None and bool(match.patch)
        Ad.objects.filter(pk=ad_id).update(**updates)

    price = data["price"]
    price_changed = price["status"] == "known" and _record_price(
        ad_id, data["observed_at"], price
    )
    outcome: SnapshotOutcome = (
        "created"
        if previous is None
        else "updated"
        if changed or price_changed
        else "unchanged"
    )
    return SnapshotResult(outcome, ad_id, property_id, price_changed)


def _record_price(ad_id: int, observed_at: datetime, price: KnownPrice) -> bool:
    pair = (price["price_rub"], price["price_kind"])
    history = PriceHistory.objects.filter(ad_id=ad_id)
    latest = (
        history.order_by("-observed_at").values_list("price_rub", "price_kind").first()
    )
    if latest == pair:
        return False
    history.create(
        ad_id=ad_id, observed_at=observed_at, price_rub=pair[0], price_kind=pair[1]
    )
    return True
