"""Render the guest catalog and property card."""

import re
from collections import Counter, defaultdict
from urllib.parse import urlencode

from django.core.paginator import EmptyPage
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_safe

from app.catalog.catalog_filters import CatalogFilters
from app.catalog.catalog_labels import (
    DEAL_LABELS,
    MARKET_LABELS,
    PRICE_UNITS,
    compact_property_location,
    property_location,
    property_title,
)
from app.catalog.catalog_queries import read_catalog
from app.catalog.location_filters import published_locations
from app.catalog.models import DealType, Property
from app.catalog.price_chart import price_chart
from app.catalog.property_details import PropertyOffer, property_details
from app.catalog.reference_defaults import (
    HOUSE_TYPE_ID,
    ROOM_TYPE_ID,
    SOURCE_LABELS,
)
from app.catalog.search_controls import (
    filter_fields,
    search_controls,
    search_error_summary,
)
from app.catalog.selection import COMPARISON, local_route, selection
from app.catalog.shortlist import saved_properties


@require_safe
def catalog(request: HttpRequest) -> HttpResponse:
    form = CatalogFilters(request.GET, rental_categories=True)
    errors = form.field_errors()
    form.set_region_choices()
    form.set_location_choices()
    path = reverse("catalog")
    deal = form.cleaned_data.get("deal_type", DealType.SALE)
    context = {
        **filter_fields(form),
        "reset_url": f"{path}?deal_type={deal}",
        "room_type_id": ROOM_TYPE_ID,
        "house_type_id": HOUSE_TYPE_ID,
    }
    if not errors:
        try:
            page, results = read_catalog(form)
        except EmptyPage:
            form.mark_missing_page()
        else:
            property_query = form.canonical_query(page=page.number, from_catalog=True)
            saved = set(saved_properties(request).get(deal, []))
            comparing = set(selection(request, COMPARISON).get(deal, []))
            cards = [
                {
                    "id": result.property.pk,
                    "property": result.property,
                    "title": property_title(result.property),
                    "address": compact_property_location(result.property),
                    "full_address": property_location(result.property),
                    "saved": result.property.pk in saved,
                    "comparing": result.property.pk in comparing,
                    "offers": [
                        {
                            "url": offer.ad.url,
                            "source_code": offer.ad.source_id,
                            "price": offer.latest_price,
                            "source": SOURCE_LABELS.get(
                                offer.ad.source_id, offer.ad.source_id
                            ),
                            "unit": PRICE_UNITS[offer.ad.deal_type],
                            "segment": MARKET_LABELS.get(offer.ad.market_segment or ""),
                        }
                        for offer in result.offers
                    ],
                    "photos": next(
                        (offer.ad.photos for offer in result.offers if offer.ad.photos),
                        [],
                    ),
                    "lead_price": min(
                        (
                            offer.latest_price
                            for offer in result.offers
                            if offer.latest_price
                        ),
                        key=lambda price: price.price_rub,
                        default=None,
                    ),
                    "url": f"{reverse('property_detail', args=[result.property.pk])}?{property_query}",
                }
                for result in results
            ]
            return render(
                request,
                "catalog/index.html",
                {
                    **context,
                    **search_controls(request, form),
                    "cards": cards,
                    "count": page.paginator.count,
                    "unit": PRICE_UNITS[deal],
                    "page": page,
                    "previous_url": f"{path}?{form.canonical_query(page=page.previous_page_number())}"
                    if page.has_previous()
                    else None,
                    "next_url": f"{path}?{form.canonical_query(page=page.next_page_number())}"
                    if page.has_next()
                    else None,
                    "has_properties": bool(cards) or Property.objects.exists(),
                    "map_url": f"{reverse('map_search')}?{urlencode(form.parameters())}",
                },
            )

    errors = form.field_errors()
    return render(
        request,
        "catalog/index.html",
        {
            **context,
            "errors": errors,
            "summary_errors": search_error_summary(errors),
            "first_page_url": f"{path}?{form.canonical_query(page=1)}"
            if errors.keys() == {"page"}
            else None,
        },
        status=400,
    )


@require_safe
def location_options(request: HttpRequest) -> JsonResponse:
    form = CatalogFilters(request.GET)
    if form.field_errors():
        return JsonResponse({}, status=400)
    return JsonResponse(
        published_locations(
            form.cleaned_data["deal_type"], form.cleaned_data["region_id"]
        )
    )


@require_safe
def property_detail(request: HttpRequest, property_id: int) -> HttpResponse:
    filters = None
    back_url = None
    back_label = None
    places = {
        "compare": ("compare", "Назад в сравнение"),
        "favorites": ("favorites", "Назад в избранное"),
        "map": ("map_search", "Назад на карту"),
    }
    origin = request.GET.getlist("from")
    if (
        len(origin) == 1
        and origin[0] in places
        and set(request.GET) == {"from", "back"}
    ):
        route, back_label = places[origin[0]]
        if len(request.GET.getlist("back")) == 1:
            back_url = local_route(request.GET["back"], {route})
        if not back_url:
            return render(
                request,
                "catalog/property_error.html",
                {"title": "Некорректная ссылка возврата"},
                status=400,
            )
    elif request.GET:
        query = request.GET.copy()
        origin = query.pop("from", [])
        filters = CatalogFilters(query)
        errors = filters.field_errors()
        if origin != ["catalog"]:
            errors["from"] = [
                {
                    "code": "repeated_parameter"
                    if len(origin) > 1
                    else "invalid_choice",
                    "message": "Для контекста поиска укажите from=catalog один раз.",
                }
            ]
        if errors:
            return render(
                request,
                "catalog/property_error.html",
                {
                    "title": "Некорректный контекст поиска",
                    "errors": list(errors.items()),
                },
                status=400,
            )

    details = property_details(property_id, filters)
    if details is None:
        return render(
            request,
            "catalog/property_error.html",
            {"title": "Объект не найден"},
            status=404,
        )

    active_offers: list[dict[str, PropertyOffer | str | None]] = []
    removed_offers: list[dict[str, PropertyOffer | str | None]] = []
    active_source_deals = Counter(
        (offer.ad.source_id, offer.ad.deal_type)
        for offer in details.offers
        if offer.ad.is_active
    )
    gallery_frames: list[dict[str, str]] = []
    histories: dict[str, list[PropertyOffer]] = defaultdict(list)
    for offer in sorted(details.offers, key=lambda offer: not offer.matches_search):
        ad = offer.ad
        target = active_offers if ad.is_active else removed_offers
        repeated_source = active_source_deals[(ad.source_id, ad.deal_type)] > 1
        source = SOURCE_LABELS.get(ad.source_id, ad.source_id)
        caption = {
            "source_code": ad.source_id,
            "source": source,
            "listing_id": ad.external_id if repeated_source else None,
            "deal": DEAL_LABELS[DealType(ad.deal_type)],
        }
        target.append(
            {
                "offer": offer,
                "description": re.sub(
                    r"\n(?:[^\S\n]*\n)+",
                    "\n\n",
                    (ad.description or "").replace("\r\n", "\n").strip(),
                ),
                **caption,
                "segment": MARKET_LABELS.get(ad.market_segment or ""),
                "unit": PRICE_UNITS[ad.deal_type],
            }
        )
        if ad.is_active:
            if offer.price_history:
                histories[ad.deal_type].append(offer)
            gallery_frames.extend(
                {"source": source, "url": url} for url in dict.fromkeys(ad.photos or [])
            )
    active_deals = {
        offer.ad.deal_type for offer in details.offers if offer.ad.is_active
    }
    saved = saved_properties(request)
    comparing = selection(request, COMPARISON)
    return render(
        request,
        "catalog/property_detail.html",
        {
            "details": details,
            "title": property_title(details.property),
            "address": property_location(details.property),
            "save_deals": [
                {
                    "id": deal,
                    "label": label,
                    "saved": property_id in saved.get(deal, []),
                    "comparing": property_id in comparing.get(deal, []),
                }
                for deal, label in DEAL_LABELS.items()
                if deal in active_deals
                or property_id in saved.get(deal, [])
                or property_id in comparing.get(deal, [])
            ],
            "back_url": back_url,
            "back_label": back_label,
            "room_type_id": ROOM_TYPE_ID,
            "house_type_id": HOUSE_TYPE_ID,
            "active_offers": active_offers,
            "multiple_offers": len(active_offers) > 1,
            "has_descriptions": any(item["description"] for item in active_offers),
            "gallery_frames": gallery_frames,
            "price_charts": [
                price_chart(offers, deal) for deal, offers in histories.items()
            ],
            "mixed_deals": len(active_deals) > 1,
            "removed_offers": removed_offers,
        },
    )
