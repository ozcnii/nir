"""Search stored property coordinates within the visible map bounds."""

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import cast
from urllib.parse import urlencode

from django.db.models import Exists, OuterRef, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_safe

from app.catalog.catalog_filters import CatalogFilters
from app.catalog.catalog_labels import (
    PRICE_UNITS,
    compact_property_location,
    property_title,
)
from app.catalog.catalog_queries import lowest_quote, matching_offers, sort_properties
from app.catalog.models import DealType, PriceKind, Property
from app.catalog.read_snapshot import read_snapshot
from app.catalog.reference_defaults import SOURCE_LABELS
from app.catalog.search_controls import (
    filter_fields,
    search_controls,
    search_error_summary,
)

MAP_LIMIT = 800
LIST_LIMIT = 24


def _bounds(value: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if len(value) > 120:
        raise ValueError
    try:
        south, west, north, east = (Decimal(number) for number in value.split(","))
    except (InvalidOperation, ValueError) as error:
        raise ValueError from error
    if not all(number.is_finite() for number in (south, west, north, east)) or not (
        -90 <= south < north <= 90
        and -180 <= west <= 180
        and -180 <= east <= 180
        and west != east
    ):
        raise ValueError
    return south, west, north, east


def _map_rows(
    filters: CatalogFilters, bounds: tuple[Decimal, Decimal, Decimal, Decimal] | None
) -> tuple[Sequence[Property], int, int, dict[int, str], dict[int, dict[str, str]]]:
    ads = matching_offers(filters)
    selected = Property.objects.filter(Exists(ads.filter(property_id=OuterRef("pk"))))
    placed = selected.filter(latitude__isnull=False, longitude__isnull=False)
    if bounds:
        south, west, north, east = bounds
        placed = placed.filter(latitude__range=(south, north))
        if west < east:
            placed = placed.filter(longitude__range=(west, east))
        else:
            placed = placed.filter(Q(longitude__gte=west) | Q(longitude__lte=east))
    quote, quote_kind = lowest_quote(ads)
    with read_snapshot():
        missing = selected.filter(latitude__isnull=True).count()
        total = placed.count()
        described = placed.select_related("region", "property_type").annotate(
            quote=quote,
            quote_kind=quote_kind,
        )
        items = list(
            sort_properties(described, filters.cleaned_data["sort"])[:MAP_LIMIT]
        )
        photos: dict[int, str] = {}
        sources: dict[int, dict[str, str]] = {}
        previews = (
            ads.filter(property__in=items[:LIST_LIMIT])
            .order_by("property_id", "source_id", "external_id")
            .values_list("property_id", "source_id", "photos__0")
        )
        for property_id, source, photo in previews:
            if photo:
                photos.setdefault(property_id, photo)
            sources.setdefault(property_id, {})[source] = SOURCE_LABELS.get(
                source, source
            )
    return items, total, missing, photos, sources


@require_safe
def map_search(request: HttpRequest) -> HttpResponse:
    query = request.GET.copy()
    raw_bounds = query.pop("bbox", [])
    filters = CatalogFilters(query, rental_categories=True)
    filters.set_region_choices()
    errors = filters.field_errors()
    filters.set_location_choices()
    unit = PRICE_UNITS[filters.cleaned_data.get("deal_type", DealType.SALE)]
    bounds = None
    if raw_bounds:
        try:
            if len(raw_bounds) != 1:
                raise ValueError
            if raw_bounds[0]:
                bounds = _bounds(raw_bounds[0])
        except ValueError:
            errors["bbox"] = [
                {
                    "code": "invalid_bounds",
                    "message": "Не удалось прочитать область поиска.",
                }
            ]
    if "page" in query:
        errors["page"] = [
            {
                "code": "invalid_scope",
                "message": "Номер страницы применяется только к списку.",
            }
        ]
    if "sort" in errors:
        errors["sort"] = [
            {
                "code": "invalid_choice",
                "message": "Не удалось прочитать порядок списка.",
            }
        ]
    scope_query = urlencode(filters.parameters())
    path = reverse("map_search")
    show_area_reset = (bounds is not None and not errors) or errors.keys() == {"bbox"}
    context = {
        **filter_fields(filters),
        "reset_url": f"{reverse('map_search')}?deal_type={filters.cleaned_data.get('deal_type', DealType.SALE)}",
        "errors": errors,
        "summary_errors": search_error_summary(errors),
        "reset_area_url": f"{path}?{scope_query}",
        "reset_area": f"{path}?{scope_query}" if show_area_reset else None,
        "bbox": raw_bounds[0] if bounds else "",
    }
    if errors:
        return render(request, "catalog/map.html", context, status=400)

    items, total, missing, photos, sources = _map_rows(filters, bounds)
    catalog_url = f"{reverse('catalog')}?{scope_query}"
    detail_query = urlencode({"from": "map", "back": request.get_full_path()})
    points = []
    for item in items:
        amount = getattr(item, "quote", None)
        points.append(
            {
                "lat": float(cast(Decimal, item.latitude)),
                "lon": float(cast(Decimal, item.longitude)),
                "title": property_title(item),
                "address": compact_property_location(item),
                "area": format(item.area_m2, "f") if item.area_m2 is not None else None,
                "amount": format(amount, "f") if amount is not None else None,
                "lower_bound": getattr(item, "quote_kind", None) == PriceKind.FROM,
                "url": f"{reverse('property_detail', args=[item.pk])}?{detail_query}",
            }
        )
    response = render(
        request,
        "catalog/map.html",
        {
            **context,
            **search_controls(request, filters, bbox=raw_bounds[0] if bounds else ""),
            "results": [
                {
                    **point,
                    "photo": photos.get(item.pk),
                    "sources": sources.get(item.pk, {}),
                }
                for item, point in zip(
                    items[:LIST_LIMIT], points[:LIST_LIMIT], strict=True
                )
            ],
            "state": {
                "points": points,
                "bounds": [float(value) for value in bounds] if bounds else None,
                "scope": scope_query,
                "unit": unit,
            },
            "catalog_url": catalog_url,
            "total": total,
            "missing": missing,
            "unit": unit,
            "capped": total > MAP_LIMIT,
            "shown_points": len(points),
            "visible_list": min(total, LIST_LIMIT),
        },
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
