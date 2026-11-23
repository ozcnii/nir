"""Build links that change applied search filters without losing the rest."""

from urllib.parse import urlencode

from django.http import HttpRequest
from django.utils.formats import number_format

from app.catalog.catalog_filters import (
    RANGE_LABELS,
    SORT_CHOICES,
    CatalogFilters,
    FieldError,
)
from app.catalog.catalog_labels import DEAL_LABELS, PRICE_UNITS
from app.catalog.location_filters import LOCATION_FIELDS
from app.catalog.models import DealType

_CATEGORY_RANGE_LABELS = {
    ("area", "house"): "Площадь дома, м²",
    ("area", "room"): "Площадь комнаты, м²",
    ("rooms", "room"): "Предлагаемых комнат",
}


def _range_label(name: str, label: str, deal: str, category: str) -> str:
    if name == "price":
        return f"Цена, {PRICE_UNITS[deal]}"
    return _CATEGORY_RANGE_LABELS.get((name, category), label)


def filter_fields(form: CatalogFilters) -> dict[str, object]:
    values = form.cleaned_data
    deal = values.get("deal_type", DealType.SALE)
    category = values.get("category", "all")
    extra_filters = form.parameters().keys() - {"deal_type", "category", "sort"}
    ranges = []
    for name, label in RANGE_LABELS:
        lower, upper = form[f"{name}_min"], form[f"{name}_max"]
        inverted = form.has_error(lower.name, "inverted_range")
        for field in (lower, upper):
            field.field.widget.attrs["aria-labelledby"] = (
                f"{lower.auto_id}-legend {field.auto_id}-label"
            )
            if inverted:
                field.field.widget.attrs["aria-describedby"] = f"{lower.auto_id}_error"
        ranges.append(
            (_range_label(name, label, deal, category), lower, upper, inverted)
        )
    return {
        "budget": ranges[0],
        "area": ranges[1],
        "ranges": ranges[2:],
        "deal": deal,
        "applied_deal": DEAL_LABELS[deal],
        "price_units": PRICE_UNITS,
        "choice_fields": [
            form[name] for name in ("deal_type", "category", "region_id")
        ],
        "location_fields": [form[name] for name in LOCATION_FIELDS],
        "selected_sort": values.get("sort"),
        "show_reset": bool(
            form.errors
            or extra_filters
            or category != "all"
            or values.get("page") != "1"
        ),
        "advanced_open": any(
            form[name].value() or form[name].errors for name in LOCATION_FIELDS
        ),
        "advanced_range_open": any(
            field.errors
            or (field.name.startswith(("area_", "floor_")) and field.value())
            for _, lower, upper, _ in ranges[1:]
            for field in (lower, upper)
        ),
    }


def search_error_summary(errors: dict[str, list[FieldError]]) -> list[FieldError]:
    visible = {"deal_type", "category", "region_id", *LOCATION_FIELDS}
    visible.update(
        f"{name}_{edge}" for name, _ in RANGE_LABELS for edge in ("min", "max")
    )
    return [
        entry
        for field, entries in errors.items()
        if field not in visible
        for entry in entries
    ]


def search_controls(
    request: HttpRequest, form: CatalogFilters, *, bbox: str = ""
) -> dict[str, object]:
    parameters = form.parameters()
    if bbox:
        parameters["bbox"] = bbox
    chips: list[tuple[str, str]] = []

    def add(label: str, *keys: str) -> None:
        remaining = {key: value for key, value in parameters.items() if key not in keys}
        chips.append((label, f"{request.path}?{urlencode(remaining)}"))

    for field in ("category", "region_id"):
        value = form.cleaned_data.get(field)
        if value and value != "all":
            choices = dict(form.fields[field].widget.choices)
            add(choices[value], field)
    for field in LOCATION_FIELDS:
        if value := parameters.get(field):
            add(f"{form[field].label} · {value}", field)
    for quantity, label in RANGE_LABELS:
        lower, upper = f"{quantity}_min", f"{quantity}_max"
        minimum, maximum = form.cleaned_data.get(lower), form.cleaned_data.get(upper)
        category = parameters["category"]
        if quantity == "rooms" and category not in {"room", "house"}:
            room_labels = {1: "1 комната", 2: "2 комнаты", 3: "3 комнаты"}
            if category != "all":
                room_labels[0] = "Студия"
            preset_label = (
                room_labels.get(minimum)
                if isinstance(minimum, int) and minimum == maximum
                else None
            )
            if minimum == 4 and maximum is None:
                preset_label = "4+ комнаты"
            if preset_label:
                add(preset_label, lower, upper)
                continue
        bounds = [
            f"{edge} {number_format(value, force_grouping=True)}"
            for key, edge in ((lower, "от"), (upper, "до"))
            if (value := form.cleaned_data.get(key)) is not None
        ]
        if bounds:
            name = _range_label(quantity, label, parameters["deal_type"], category)
            add(f"{name} {' '.join(bounds)}", lower, upper)
    sort = parameters.get("sort", "")
    unsorted = {key: value for key, value in parameters.items() if key != "sort"}
    return {
        "filter_chips": chips,
        "sort_label": dict(SORT_CHOICES)[sort],
        "sort_links": [
            (
                label,
                f"{request.path}?{urlencode({**unsorted, **({'sort': key} if key else {})})}",
                key == sort,
            )
            for key, label in SORT_CHOICES
        ],
    }
