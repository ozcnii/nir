"""Render and manage the visitor's comparison workspace."""

from collections.abc import Hashable, Sequence
from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations
from typing import cast
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from app.catalog.catalog_filters import BIGINT_MAX, CatalogFilters, DecimalFilter
from app.catalog.catalog_labels import (
    DEAL_LABELS,
    PRICE_UNITS,
    compact_property_location,
)
from app.catalog.catalog_queries import CatalogResult, read_catalog, read_selected
from app.catalog.comparison import (
    MAX_COMPARED,
    ComparedProperty,
    recommendation,
    selected_property,
)
from app.catalog.models import Ad, DealType, PriceKind
from app.catalog.reference_defaults import HOUSE_TYPE_ID, ROOM_TYPE_ID
from app.catalog.selection import (
    COMPARISON,
    FAVORITES,
    SelectionForm,
    edit_mapping,
    edit_selection,
    local_route,
    selection,
)

OPTIONS = "catalog_comparison_options"

PRIORITIES = {
    "price": "Цене",
    "area": "Площади",
    "per_meter": "Цене за м²",
}


class ComparisonOptions(forms.Form):
    deal = forms.ChoiceField(choices=DEAL_LABELS.items())
    priority = forms.ChoiceField(choices=PRIORITIES.items(), required=False)
    budget = DecimalFilter("99999999999999.99")
    differences = forms.BooleanField(required=False, widget=forms.HiddenInput)
    pair = forms.CharField(max_length=42, required=False)


def _property_ids(values: list[str]) -> list[int] | None:
    if not 2 <= len(values) <= MAX_COMPARED:
        return None
    field = forms.IntegerField(min_value=1, max_value=BIGINT_MAX)
    try:
        ids = [field.clean(value) for value in values]
    except ValidationError:
        return None
    return ids if len(set(ids)) == len(ids) else None


def _configure(request: HttpRequest) -> HttpResponse:
    deal = request.POST.get("deal", "")
    updates = {
        key: value
        for key in ("pair", "differences")
        if (value := request.POST.get(f"set_{key}")) is not None
    }
    if not updates:
        updates = {key: request.POST.get(key, "") for key in ("priority", "budget")}
    empty: dict[str, str] = {}
    with edit_mapping(request, OPTIONS, empty) as options:
        saved = options.get(deal)
        data = QueryDict(saved, mutable=True) if saved else request.POST.copy()
        for key, value in updates.items():
            data[key] = value
        form = ComparisonOptions(data)
        query = urlencode(
            [(key, value) for key in form.fields if (value := data.get(key))]
        )
        if form.is_valid():
            options[deal] = query
    return redirect(f"{reverse('compare')}?{query}")


def _change(request: HttpRequest) -> HttpResponse:
    action = request.POST.get("action")
    if action == "import":
        deal = request.POST.get("deal", "")
        ids = _property_ids(request.POST.getlist("property_id"))
        if deal not in DEAL_LABELS or ids is None:
            return HttpResponse("Некорректная подборка", status=400)
    else:
        form = SelectionForm(request.POST)
        if not form.is_valid():
            return HttpResponse("Некорректное действие со сравнением", status=400)
        deal = form.cleaned_data["deal"]
        ids = [form.cleaned_data["property_id"]]

    with edit_selection(request, COMPARISON) as comparing:
        selected = comparing.get(deal, [])
        if action == "remove":
            remaining = [item for item in selected if item not in ids]
            if remaining:
                comparing[deal] = remaining
            else:
                comparing.pop(deal, None)
        else:
            active = set(
                Ad.objects.filter(
                    property_id__in=ids, deal_type=deal, is_active=True
                ).values_list("property_id", flat=True)
            )
            merged = list(
                dict.fromkeys([*selected, *(item for item in ids if item in active)])
            )
            if len(merged) > MAX_COMPARED:
                messages.error(
                    request,
                    f"В сравнении уже {MAX_COMPARED} объектов. Освободите место для нового.",
                    extra_tags="compare-limit",
                )
            else:
                if merged != selected:
                    comparing.pop(deal, None)
                    comparing[deal] = merged
                if len(active) < len(ids):
                    messages.warning(
                        request,
                        "В сравнение вошли только объекты с активными объявлениями.",
                    )
    destination = local_route(
        request.POST.get("return_to", ""),
        {"catalog", "property_detail", "favorites", "compare"},
    )
    return redirect(destination or f"{reverse('compare')}?{urlencode({'deal': deal})}")


def _budget_status(item: ComparedProperty, budget: Decimal) -> str:
    if any(
        offer.price
        and offer.price.price_kind == PriceKind.LISTED
        and offer.price.price_rub <= budget
        for offer in item.offers
    ):
        return "within"
    if item.offers and all(
        offer.price and offer.price.price_rub > budget for offer in item.offers
    ):
        return "over"
    return "unknown"


def _rows(
    items: list[ComparedProperty], visible: list[ComparedProperty]
) -> dict[str, dict[str, bool]]:
    assets = [item.asset for item in visible]
    values: dict[str, Sequence[Hashable]] = {
        "price": [
            (price.price_kind, price.price_rub) if price is not None else None
            for price in (item.lead_price for item in visible)
        ],
        "address": [item.address for item in visible],
        "area": [asset.area_m2 if asset else None for asset in assets],
        "rooms": [
            (asset.property_type_id, asset.rooms)
            if asset and asset.rooms is not None
            else None
            for asset in assets
        ],
        "floor": [
            (asset.floor, asset.floors_total)
            if asset and asset.floor is not None
            else None
            for asset in assets
        ],
        "land": [asset.land_area_m2 if asset else None for asset in assets],
        "per_meter": [
            price.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if price is not None
            else None
            for price in (item.lead_price_per_meter for item in visible)
        ],
    }
    supported = {
        "land": any(
            item.asset is not None and item.asset.land_area_m2 is not None
            for item in items
        ),
        "per_meter": any(item.lead_price_per_meter is not None for item in items),
    }
    return {
        name: {
            "visible": supported.get(name, True),
            "different": len(set(entries)) > 1,
        }
        for name, entries in values.items()
    }


@require_http_methods(["GET", "HEAD", "POST"])
def compare(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        if request.POST.get("action") == "configure":
            return _configure(request)
        return _change(request)

    path = reverse("compare")
    query = request.GET.copy()
    current = selection(request, COMPARISON)
    query.setdefault("deal", next(reversed(current), DealType.SALE))
    if set(query) == {"deal"} and len(query.getlist("deal")) == 1:
        preferences = cast(dict[str, str], request.session.get(OPTIONS, {}))
        if saved := preferences.get(query.getlist("deal")[0]):
            query = QueryDict(saved)
    options = ComparisonOptions(query)
    shared = "property_id" in query
    explicit_ids = _property_ids(query.getlist("property_id")) if shared else []
    options.is_valid()
    if (
        options.errors.keys() - {"budget"}
        or any(len(query.getlist(key)) != 1 for key in options.fields if key in query)
        or explicit_ids is None
    ):
        return render(
            request,
            "catalog/compare.html",
            {"invalid_selection": True, "options": options, "limit": MAX_COMPARED},
            status=400,
        )

    deal: str = options.cleaned_data["deal"]
    ids = explicit_ids if shared else current.get(deal, [])
    favorite_ids = selection(request, FAVORITES).get(deal, [])
    found = read_selected(deal, list(dict.fromkeys([*ids, *favorite_ids[:12]])))
    items = [selected_property(item, found.get(item), deal) for item in ids]
    candidates = [
        selected_property(item, found[item], deal)
        for item in favorite_ids[:12]
        if item not in ids and item in found and found[item].offers
    ]

    suggestions: list[ComparedProperty] = []
    if not shared and len(items) < 2 and len(candidates) < 2:
        filters = CatalogFilters(QueryDict(urlencode({"deal_type": deal})))
        filters.full_clean()
        _, catalog = read_catalog(filters)
        excluded = {*ids, *favorite_ids}
        first_by_source: dict[str, CatalogResult] = {}
        remaining: list[CatalogResult] = []
        for result in catalog:
            if result.property.pk in excluded:
                continue
            source = result.offers[0].ad.source_id
            if source in first_by_source:
                remaining.append(result)
            else:
                first_by_source[source] = result
        suggestions = [
            selected_property(result.property.pk, result, deal)
            for result in [*first_by_source.values(), *remaining][:3]
        ]

    pairs = {
        f"{left.id},{right.id}": (f"№ {first} и № {second}", [left, right])
        for (first, left), (second, right) in combinations(enumerate(items, 1), 2)
    }
    requested_pair = options.cleaned_data["pair"]
    pair = requested_pair if len(items) > 2 and requested_pair in pairs else ""
    visible = pairs[pair][1] if pair else items
    budget: Decimal | None = options.cleaned_data.get("budget")
    budget_errors = options.errors.get("budget")
    budget_by_id = (
        {item.id: _budget_status(item, budget) for item in visible}
        if budget is not None
        else {}
    )
    ranked = [
        item for item in visible if budget is None or budget_by_id[item.id] == "within"
    ]
    meter = any(item.lead_price_per_meter is not None for item in items)
    priorities = {
        key: label for key, label in PRIORITIES.items() if key != "per_meter" or meter
    }
    priority = options.cleaned_data["priority"] or "price"
    priority = priority if priority in priorities else "price"
    differences = options.cleaned_data["differences"]

    view_options: dict[str, str | int] = {"deal": deal, "priority": priority}
    if budget is not None:
        view_options["budget"] = str(budget)
    if differences:
        view_options["differences"] = 1
    if len(visible) < len(items):
        view_options["pair"] = pair
    view_query = list(view_options.items())

    def presentation_url(**changes: str | int) -> str:
        parameters = {**view_options, **changes}
        pairs = [(key, value) for key, value in parameters.items() if value]
        if shared:
            pairs.extend(("property_id", item) for item in ids)
        return f"{path}?{urlencode(pairs)}"

    unit = PRICE_UNITS[deal].replace("руб.", "₽")
    suggestion = (
        recommendation(ranked, priority, budget=budget is not None)
        if len(items) >= 2 and not budget_errors
        else {}
    )
    best = cast(ComparedProperty | None, suggestion.get("best"))
    regions = {item.asset.region.name for item in items if item.asset is not None}
    common_region = next(iter(regions)) if len(regions) == 1 else ""
    return render(
        request,
        "catalog/compare.html",
        {
            "deal": deal,
            "shared_deal_label": DEAL_LABELS[DealType(deal)],
            "deal_tabs": [
                (
                    label,
                    f"{path}?{urlencode({'deal': key})}",
                    key == deal,
                    len(current.get(key, [])),
                )
                for key, label in (
                    (DealType.SALE, "Продажа"),
                    (DealType.MONTH_RENT, "На месяц"),
                    (DealType.DAY_RENT, "Посуточно"),
                )
            ],
            "unit": unit,
            "shared": shared,
            "view_query": view_query,
            "workspace_url": f"{path}?{urlencode(view_query)}",
            "detail_query": urlencode({"from": "compare", "back": presentation_url()}),
            "search_url": f"{reverse('catalog')}?{urlencode({'deal_type': deal})}",
            "limit": MAX_COMPARED,
            "priorities": priorities,
            "priority": priority,
            "recommendation": suggestion,
            "recommended_number": ids.index(best.id) + 1 if best is not None else None,
            "metric_unit": {"price": unit, "area": "м²", "per_meter": "руб./м²"}[
                priority
            ],
            "rows": _rows(items, visible),
            "common_region": common_region,
            "room_type_id": ROOM_TYPE_ID,
            "house_type_id": HOUSE_TYPE_ID,
            "items": items,
            "visible_items": visible,
            "visible_rows": [
                {
                    "number": ids.index(item.id) + 1,
                    "item": item,
                    "status": budget_by_id.get(item.id),
                    "address": compact_property_location(item.asset, show_region=False)
                    if item.asset is not None
                    else "Адрес не указан",
                    "region": item.asset.region.name
                    if not common_region and item.asset is not None
                    else "",
                }
                for item in visible
            ],
            "pair_links": [
                (label, key, presentation_url(pair=key), key == pair)
                for key, label in [
                    ("", "Все варианты"),
                    *((key, value[0]) for key, value in pairs.items()),
                ]
            ]
            if len(items) > 2
            else [],
            "pair_label": pairs[pair][0] if pair else "Все варианты",
            "differences_url": presentation_url(differences=0 if differences else 1),
            "pair": pair,
            "budget": budget,
            "budget_errors": budget_errors,
            "budget_value": options["budget"].value() or "",
            "counts": {
                status: list(budget_by_id.values()).count(status)
                for status in ("within", "over", "unknown")
            },
            "show_differences": differences,
            "candidates": candidates,
            "suggestions": suggestions,
        },
        status=400 if options.errors else 200,
    )
