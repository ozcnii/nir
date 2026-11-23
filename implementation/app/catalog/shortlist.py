"""Store a guest shortlist in the existing Django session."""

from urllib.parse import urlencode

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from app.catalog.catalog_labels import DEAL_TABS, PRICE_UNITS
from app.catalog.catalog_queries import read_selected
from app.catalog.comparison import selected_property
from app.catalog.models import Ad, DealType
from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    HOUSE_TYPE_ID,
    ROOM_TYPE_ID,
)
from app.catalog.selection import (
    COMPARISON,
    FAVORITES,
    SelectionForm,
    edit_selection,
    local_route,
    selection,
)

HOUSING_TABS = {
    "flat": (APARTMENT_TYPE_ID, "Квартиры"),
    "room": (ROOM_TYPE_ID, "Комнаты"),
    "house": (HOUSE_TYPE_ID, "Дома"),
}


def saved_properties(request: HttpRequest) -> dict[str, list[int]]:
    return selection(request, FAVORITES)


@require_http_methods(["GET", "HEAD", "POST"])
def favorites(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        change = SelectionForm(request.POST)
        if not change.is_valid():
            return HttpResponse("Некорректное действие с избранным", status=400)
        deal: str = change.cleaned_data["deal"]
        property_id: int = change.cleaned_data["property_id"]
        with edit_selection(request, FAVORITES) as saved:
            selected = saved.get(deal, [])
            if change.cleaned_data["action"] == "remove":
                saved[deal] = [item for item in selected if item != property_id]
            elif property_id not in selected:
                if Ad.objects.filter(
                    property_id=property_id, deal_type=deal, is_active=True
                ).exists():
                    saved[deal] = [*selected, property_id]
                else:
                    messages.error(
                        request,
                        "У объекта больше нет активных предложений по этой сделке.",
                    )
        destination = local_route(
            request.POST.get("return_to", ""),
            {"catalog", "property_detail", "favorites"},
        )
        return redirect(destination or reverse("favorites"))

    saved = saved_properties(request)
    path = reverse("favorites")
    deal = request.GET.get("deal", "")
    if deal not in DEAL_TABS:
        deal = next((kind for kind in DEAL_TABS if saved.get(kind)), DealType.SALE)
    housing = request.GET.get("category", "")
    type_id = HOUSING_TABS[housing][0] if housing in HOUSING_TABS else None
    deal_url = f"{path}?{urlencode({'deal': deal})}"
    list_url = f"{deal_url}&{urlencode({'category': housing})}" if type_id else deal_url
    ids = saved.get(deal, [])
    found = read_selected(deal, ids) if ids else {}
    comparing = selection(request, COMPARISON)
    detail_query = urlencode({"from": "favorites", "back": list_url})
    cards = []
    for property_id in ids:
        result = found.get(property_id)
        if type_id is not None and (
            result is None or result.property.property_type_id != type_id
        ):
            continue
        item = selected_property(property_id, result, deal)
        cards.append(
            {
                "id": item.id,
                "property": item.asset,
                "title": item.title,
                "address": item.short_address,
                "full_address": item.address,
                "offers": item.offers,
                "photos": item.photos,
                "lead_price": item.lead_price,
                "url": f"{item.url}?{detail_query}" if item.url else "",
                "saved": True,
                "comparing": item.id in comparing.get(deal, []),
            }
        )
    types = {result.property.property_type_id for result in found.values()}
    housing_tabs = []
    if len(types) > 1 or type_id is not None:
        housing_tabs = [("Всё жильё", deal_url, type_id is None, 0)]
        housing_tabs.extend(
            (
                label,
                f"{deal_url}&{urlencode({'category': key})}",
                type_id == identifier,
                0,
            )
            for key, (identifier, label) in HOUSING_TABS.items()
            if identifier in types or type_id == identifier
        )
    return render(
        request,
        "catalog/favorites.html",
        {
            "deal_tabs": [
                (
                    label,
                    f"{path}?{urlencode({'deal': key})}",
                    key == deal,
                    len(saved.get(key, [])),
                )
                for key, label in DEAL_TABS.items()
            ],
            "housing_tabs": housing_tabs,
            "has_saved": any(saved.values()),
            "search_url": f"{reverse('catalog')}?{urlencode({'deal_type': deal, 'category': housing if type_id else 'all'})}",
            "deal": deal,
            "unit": PRICE_UNITS[deal],
            "cards": cards,
            "room_type_id": ROOM_TYPE_ID,
            "house_type_id": HOUSE_TYPE_ID,
        },
    )
