"""Summarize the published prices in the selected catalogue segment."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from statistics import median, quantiles

from django.db.models import OuterRef, Subquery
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_safe

from app.catalog.catalog_filters import CatalogFilters
from app.catalog.catalog_labels import PRICE_UNITS
from app.catalog.catalog_queries import matching_offers
from app.catalog.models import PriceHistory, PriceKind
from app.catalog.read_snapshot import read_snapshot
from app.catalog.reference_defaults import SOURCE_LABELS

SCOPE_FIELDS = ("deal_type", "category", "region_id")


@dataclass(frozen=True)
class SourceSummary:
    name: str
    total: int
    exact_count: int
    median_price: Decimal | None
    code: str


def _summarize(filters: CatalogFilters) -> dict[str, object]:
    latest = PriceHistory.objects.filter(ad_id=OuterRef("pk")).order_by("-observed_at")
    offers = (
        matching_offers(filters)
        .order_by()
        .annotate(
            amount=Subquery(latest.values("price_rub")[:1]),
            kind=Subquery(latest.values("price_kind")[:1]),
        )
    )
    sources: Counter[str] = Counter()
    exact: defaultdict[str, list[Decimal]] = defaultdict(list)
    objects: set[int] = set()
    lower_bounds = 0
    with read_snapshot():
        for source, item, amount, kind in offers.values_list(
            "source_id", "property_id", "amount", "kind"
        ).iterator():
            sources[source] += 1
            objects.add(item)
            if kind == PriceKind.LISTED and amount is not None:
                exact[source].append(amount)
            elif kind == PriceKind.FROM:
                lower_bounds += 1
    amounts = [value for prices in exact.values() for value in prices]
    quartiles = (
        quantiles(amounts, n=4, method="inclusive") if len(amounts) > 1 else None
    )
    return {
        "total": sources.total(),
        "objects": len(objects),
        "exact_count": len(amounts),
        "lower_bounds": lower_bounds,
        "unknown_count": sources.total() - len(amounts) - lower_bounds,
        "median_price": median(amounts) if amounts else None,
        "middle": (quartiles[0], quartiles[2]) if quartiles else None,
        "sources": [
            SourceSummary(
                SOURCE_LABELS.get(source, source),
                count,
                len(exact[source]),
                median(exact[source]) if exact[source] else None,
                source,
            )
            for source, count in sources.most_common()
        ],
    }


@require_safe
def market(request: HttpRequest) -> HttpResponse:
    query = request.GET.copy()
    query.setdefault("category", "flat")
    filters = CatalogFilters(query, rental_categories=True)
    filters.set_region_choices()
    errors = filters.field_errors()
    if query.keys() - set(SCOPE_FIELDS):
        errors["scope"] = [
            {
                "code": "invalid_scope",
                "message": "В обзоре доступны сделка, категория и регион.",
            }
        ]
    context = {"fields": [filters[name] for name in SCOPE_FIELDS], "errors": errors}
    if errors:
        return render(request, "catalog/market.html", context, status=400)
    return render(
        request,
        "catalog/market.html",
        {
            **context,
            "summary": _summarize(filters),
            "unit": PRICE_UNITS[filters.cleaned_data["deal_type"]],
            "catalog_url": f"{reverse('catalog')}?{filters.canonical_query(page=1)}",
        },
    )
