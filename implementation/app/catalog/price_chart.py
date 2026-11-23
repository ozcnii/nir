"""Plot observed asking prices on a shared scale for each deal type."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.catalog.catalog_labels import DEAL_LABELS, PRICE_UNITS
from app.catalog.models import DealType, PriceHistory, PriceKind
from app.catalog.property_details import PropertyOffer
from app.catalog.reference_defaults import SOURCE_LABELS


@dataclass(frozen=True)
class ChartPoint:
    price: PriceHistory
    x: float
    y: float


@dataclass(frozen=True)
class ChartSeries:
    source: str
    label: str
    points: list[ChartPoint]

    @property
    def coordinates(self) -> str:
        return " ".join(f"{point.x:.2f},{point.y:.2f}" for point in self.points)


@dataclass(frozen=True)
class ChartMarker:
    x: float
    y: float
    rings: list[tuple[str, str, float]]
    observations: list[tuple[str, PriceHistory]]


@dataclass(frozen=True)
class PriceChart:
    label: str
    unit: str
    series: list[ChartSeries]
    markers: list[ChartMarker]
    price_ticks: list[tuple[Decimal, float]]
    date_ticks: list[tuple[datetime, float]]
    has_estimates: bool
    point_count: int


def _price_ticks(prices: list[PriceHistory]) -> list[Decimal]:
    lowest = min(price.price_rub for price in prices)
    highest = max(price.price_rub for price in prices)
    span = highest - lowest or max(highest / 10, Decimal("0.10"))
    raw_step = span / 4
    magnitude = Decimal(1).scaleb(raw_step.adjusted())
    step = max(
        next(
            magnitude * factor
            for factor in (1, 2, 5, 10)
            if magnitude * factor >= raw_step
        ),
        Decimal("0.01"),
    )
    start = int((lowest / step).to_integral_value(rounding=ROUND_FLOOR))
    end = int((highest / step).to_integral_value(rounding=ROUND_CEILING))
    if lowest == highest:
        start = max(start - 1, 0)
        end += 1
    return [index * step for index in range(start, end + 1)]


def _markers(series: list[ChartSeries]) -> list[ChartMarker]:
    positions: dict[tuple[float, float], list[tuple[ChartSeries, PriceHistory]]] = (
        defaultdict(list)
    )
    for line in series:
        for point in line.points:
            positions[(point.x, point.y)].append((line, point.price))

    markers = []
    for (x, y), coincident in sorted(positions.items()):
        styles = dict.fromkeys(
            (line.source, price.price_kind) for line, price in coincident
        )
        outer_first = sorted(styles, key=lambda style: style[1] == PriceKind.LISTED)
        outer_radius = 4 + 2 * (len(outer_first) - 1)
        markers.append(
            ChartMarker(
                x=x,
                y=y,
                rings=[
                    (source, kind, outer_radius - 2 * index)
                    for index, (source, kind) in enumerate(outer_first)
                ],
                observations=[(line.label, price) for line, price in coincident],
            )
        )
    return markers


def price_chart(offers: list[PropertyOffer], deal: str) -> PriceChart:
    observations = [price for offer in offers for price in offer.price_history]
    ticks = _price_ticks(observations)
    first = min(price.observed_at for price in observations)
    last = max(price.observed_at for price in observations)

    def x(timestamp: datetime) -> float:
        if first == last:
            return 324.0
        return 158 + 332 * ((timestamp - first) / (last - first))

    def y(amount: Decimal) -> float:
        return 114 - 98 * float((amount - ticks[0]) / (ticks[-1] - ticks[0]))

    counts = Counter(offer.ad.source_id for offer in offers)

    def series_label(offer: PropertyOffer) -> str:
        ad = offer.ad
        label = SOURCE_LABELS.get(ad.source_id, ad.source_id)
        return f"{label} № {ad.external_id}" if counts[ad.source_id] > 1 else label

    series = [
        ChartSeries(
            source=offer.ad.source_id,
            label=series_label(offer),
            points=[
                ChartPoint(price=price, x=x(price.observed_at), y=y(price.price_rub))
                for price in offer.price_history
            ],
        )
        for offer in offers
    ]
    return PriceChart(
        label=DEAL_LABELS[DealType(deal)],
        unit=PRICE_UNITS[deal],
        series=series,
        markers=_markers(series),
        price_ticks=[(amount, y(amount)) for amount in ticks],
        date_ticks=(
            [(first, 324.0)]
            if first.date() == last.date()
            else [(first, x(first)), (last, x(last))]
        ),
        has_estimates=any(price.price_kind == PriceKind.FROM for price in observations),
        point_count=len(observations),
    )
