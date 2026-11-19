"""Render the guest catalog from the same filters used in its links."""

from typing import cast

from django import forms
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_safe

from app.catalog.catalog_filters import RANGE_LABELS, CatalogFilters
from app.catalog.catalog_labels import MARKET_LABELS, PRICE_UNITS, property_title
from app.catalog.catalog_queries import catalog_page, catalog_results
from app.catalog.models import DealType, Property, Region
from app.catalog.reference_defaults import (
    HOUSE_TYPE_ID,
    ROOM_TYPE_ID,
    SOURCE_LABELS,
)


@require_safe
def catalog(request: HttpRequest) -> HttpResponse:
    form = CatalogFilters(request.GET)
    errors = form.field_errors()
    region_widget = cast(forms.Select, form.fields["region_id"].widget)
    region_widget.choices = [
        ("", "Все регионы"),
        *Region.objects.order_by("name").values_list("id", "name"),
    ]
    path = reverse("catalog")
    deal = form.cleaned_data.get("deal_type", DealType.SALE)
    category = form.cleaned_data.get("category", "all")
    ranges = [
        (
            f"{label}, {PRICE_UNITS[deal]}"
            if quantity == "price"
            else "Предлагаемых комнат"
            if quantity == "rooms" and category == "room"
            else label,
            form[f"{quantity}_min"],
            form[f"{quantity}_max"],
        )
        for quantity, label in RANGE_LABELS
    ]
    context = {
        "form": form,
        "ranges": ranges,
        "choice_fields": [
            form[name] for name in ("deal_type", "category", "region_id")
        ],
        "reset_url": f"{path}?deal_type={deal}",
        "room_type_id": ROOM_TYPE_ID,
        "house_type_id": HOUSE_TYPE_ID,
    }
    if not errors:
        try:
            page = catalog_page(form)
        except EmptyPage:
            form.add_error(
                "page", ValidationError("Такой страницы уже нет.", code="out_of_range")
            )
        else:
            canonical = form.canonical_query(page=page.number)
            property_query = form.canonical_query(page=page.number, from_catalog=True)
            cards = [
                {
                    "property": result.property,
                    "title": property_title(result.property),
                    "offers": [
                        {
                            "ad": offer.ad,
                            "price": offer.latest_price,
                            "source": SOURCE_LABELS.get(
                                offer.ad.source_id, offer.ad.source_id
                            ),
                            "unit": PRICE_UNITS[offer.ad.deal_type],
                            "segment": MARKET_LABELS.get(offer.ad.market_segment or ""),
                        }
                        for offer in result.offers
                    ],
                    "photo": next(
                        (
                            offer.ad.photos[0]
                            for offer in result.offers
                            if offer.ad.photos
                        ),
                        None,
                    ),
                    "url": f"/properties/{result.property.pk}/?{property_query}",
                }
                for result in catalog_results(page, form)
            ]
            return render(
                request,
                "catalog/index.html",
                {
                    **context,
                    "cards": cards,
                    "count": page.paginator.count,
                    "json_url": f"/api/properties/?{canonical}",
                    "page": page,
                    "previous_url": f"{path}?{form.canonical_query(page=page.previous_page_number())}"
                    if page.has_previous()
                    else None,
                    "next_url": f"{path}?{form.canonical_query(page=page.next_page_number())}"
                    if page.has_next()
                    else None,
                    "has_properties": bool(cards) or Property.objects.exists(),
                },
            )

    return render(
        request,
        "catalog/index.html",
        {
            **context,
            "errors": list(form.field_errors().items()),
            "first_page_url": f"{path}?{form.canonical_query(page=1)}",
        },
        status=400,
    )
