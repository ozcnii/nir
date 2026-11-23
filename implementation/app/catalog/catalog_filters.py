"""Parse the shared URL filters for the public catalog and property links."""

import re
from decimal import Decimal
from typing import TypedDict, cast
from urllib.parse import urlencode

from django import forms
from django.core.exceptions import ValidationError
from django.http import QueryDict

from app.catalog.catalog_labels import DEAL_LABELS
from app.catalog.location_filters import (
    LOCATION_FIELDS,
    LOCATION_NAME_LIMIT,
    published_locations,
)
from app.catalog.models import DealType, Region

RANGE_LABELS = (
    ("price", "Последняя известная опубликованная цена"),
    ("area", "Площадь, м²"),
    ("rooms", "Комнаты"),
    ("floor", "Этаж"),
)
SORT_CHOICES = (
    ("", "Порядок каталога"),
    ("price_asc", "Сначала дешевле"),
    ("price_desc", "Сначала дороже"),
    ("area_desc", "От большей площади"),
    ("area_asc", "От меньшей площади"),
)

BIGINT_MAX = 2**63 - 1
SMALLINT_MAX = 2**15 - 1
INTEGER = re.compile(r"[0-9]+")
SIGNED_INTEGER = re.compile(r"-?[0-9]+")
DECIMAL = re.compile(r"[0-9]+(?:[.,][0-9]{1,2})?")
SALE_CATEGORIES = {"resale", "new_build"}


class FieldError(TypedDict):
    code: str
    message: str


def _integer(value: str, minimum: int, maximum: int) -> int:
    pattern = SIGNED_INTEGER if minimum < 0 else INTEGER
    if not pattern.fullmatch(value):
        raise ValidationError("Введите целое число.", code="invalid_integer")
    normalized = value.lstrip("-").lstrip("0") or "0"
    if len(normalized) > 19:
        raise ValidationError("Число вне допустимого диапазона.", code="out_of_range")
    number = int(normalized)
    if value.startswith("-"):
        number = -number
    if not minimum <= number <= maximum:
        raise ValidationError("Число вне допустимого диапазона.", code="out_of_range")
    return number


class IntegerFilter(forms.Field):
    def __init__(self, minimum: int, maximum: int) -> None:
        super().__init__(
            required=False,
            widget=forms.TextInput(
                attrs={
                    "inputmode": "text" if minimum < 0 else "numeric",
                    "data-number-kind": "signed" if minimum < 0 else "integer",
                    "data-number-min": minimum,
                    "data-number-max": maximum,
                }
            ),
        )
        self.minimum = minimum
        self.maximum = maximum

    def to_python(self, value: str | None) -> int | None:
        return (
            _integer(value.strip(), self.minimum, self.maximum)
            if value and value.strip()
            else None
        )


class DecimalFilter(forms.Field):
    def __init__(self, maximum: str) -> None:
        super().__init__(
            required=False,
            widget=forms.TextInput(
                attrs={
                    "inputmode": "decimal",
                    "data-number-kind": "decimal",
                    "data-number-min": 0,
                    "data-number-max": maximum,
                }
            ),
        )
        self.maximum = Decimal(maximum)

    def to_python(self, value: str | None) -> Decimal | None:
        if not value or not value.strip():
            return None
        value = value.strip()
        if not DECIMAL.fullmatch(value):
            raise ValidationError(
                "Введите число с точностью до двух знаков.", code="invalid_decimal"
            )
        number = Decimal(value.replace(",", "."))
        if number > self.maximum:
            raise ValidationError(
                "Число вне допустимого диапазона.", code="out_of_range"
            )
        return number


class PageFilter(forms.Field):
    def to_python(self, value: str | None) -> str:
        if not value or not INTEGER.fullmatch(value.strip()):
            raise ValidationError("Введите номер страницы.", code="invalid_integer")
        canonical = value.strip().lstrip("0")
        if not canonical:
            raise ValidationError(
                "Номер страницы должен быть больше нуля.", code="out_of_range"
            )
        return canonical


class RegionFilter(forms.Field):
    def __init__(self) -> None:
        super().__init__(label="Регион", required=False, widget=forms.Select)

    def prepare_value(self, value: object) -> object:
        return value.strip().lstrip("0") if isinstance(value, str) else value

    def to_python(self, value: str | None) -> int | None:
        if not value or not value.strip():
            return None
        region_id = _integer(value.strip(), 1, BIGINT_MAX)
        if not Region.objects.filter(pk=region_id).exists():
            raise ValidationError("Регион не найден.", code="not_found")
        return region_id


class CategorySelect(forms.Select):
    rental = False

    def create_option(
        self,
        name: str,
        value: object,
        label: int | str,
        selected: bool,
        index: int,
        subindex: int | None = None,
        attrs: dict[str, object] | None = None,
    ) -> dict[str, object]:
        option = super().create_option(
            name, value, label, selected, index, subindex, attrs
        )
        if value in SALE_CATEGORIES:
            option["attrs"]["data-sale-only"] = True
            if self.rental:
                option["attrs"].update(hidden=True, disabled=True)
        return option


class CatalogFilters(forms.Form):
    deal_type = forms.ChoiceField(
        label="Сделка",
        choices=DEAL_LABELS.items(),
        error_messages={
            "required": "Выберите вид сделки.",
            "invalid_choice": "Выберите вид сделки из списка.",
        },
    )
    category = forms.ChoiceField(
        label="Тип жилья",
        required=False,
        widget=CategorySelect,
        choices=[
            ("all", "Всё жильё"),
            ("flat", "Квартиры"),
            ("resale", "Вторичка"),
            ("new_build", "Новостройка"),
            ("room", "Комнаты"),
            ("house", "Дома"),
        ],
        error_messages={"invalid_choice": "Выберите категорию из списка."},
    )
    region_id = RegionFilter()
    district = forms.CharField(
        label="Район",
        required=False,
        max_length=LOCATION_NAME_LIMIT,
        widget=forms.Select,
    )
    metro = forms.CharField(
        label="Метро",
        required=False,
        max_length=LOCATION_NAME_LIMIT,
        widget=forms.Select,
    )
    price_min = DecimalFilter("99999999999999.99")
    price_max = DecimalFilter("99999999999999.99")
    area_min = DecimalFilter("9999999999.99")
    area_max = DecimalFilter("9999999999.99")
    rooms_min = IntegerFilter(0, SMALLINT_MAX)
    rooms_max = IntegerFilter(0, SMALLINT_MAX)
    floor_min = IntegerFilter(-SMALLINT_MAX - 1, SMALLINT_MAX)
    floor_max = IntegerFilter(-SMALLINT_MAX - 1, SMALLINT_MAX)
    sort = forms.ChoiceField(
        choices=SORT_CHOICES,
        required=False,
        label="Сортировка",
        error_messages={"invalid_choice": "Выберите порядок сортировки из списка."},
    )
    page = PageFilter(required=False)

    def __init__(self, query: QueryDict, *, rental_categories: bool = False) -> None:
        self.query = query
        values = query.copy()
        if "deal_type" not in values:
            values["deal_type"] = DealType.SALE
        rental = rental_categories and values["deal_type"] in (
            DealType.MONTH_RENT,
            DealType.DAY_RENT,
        )
        if rental and values.get("category") in SALE_CATEGORIES:
            values["category"] = "flat"
        if not values.get("category"):
            values["category"] = "all"
        if "page" not in values:
            values["page"] = "1"
        super().__init__(values)
        cast(CategorySelect, self.fields["category"].widget).rental = rental

    def set_region_choices(self) -> None:
        widget = cast(forms.Select, self.fields["region_id"].widget)
        widget.choices = [
            ("", "Все регионы"),
            *Region.objects.order_by("name").values_list("id", "name"),
        ]

    def set_location_choices(self) -> None:
        names = published_locations(
            self.cleaned_data.get("deal_type", DealType.SALE),
            self.cleaned_data.get("region_id"),
        )
        for field, options in names.items():
            selected = self.cleaned_data.get(field, "")
            if selected and selected not in options:
                options.append(selected)
            widget = cast(forms.Select, self.fields[field].widget)
            empty_label = "Любой район" if field == "district" else "Любая станция"
            widget.choices = [
                ("", empty_label if options else "Нет данных"),
                *((name, name) for name in options),
            ]

    def clean(self) -> dict[str, object]:
        values = super().clean() or {}
        for name, items in self.query.lists():
            if name not in self.fields:
                continue
            if len(items) != 1:
                self.errors.pop(name, None)
                self.add_error(
                    name,
                    ValidationError(
                        "Параметр указан несколько раз.", code="repeated_parameter"
                    ),
                )
        if self.has_error("deal_type", "required"):
            self.errors.pop("deal_type")
            self.add_error(
                "deal_type",
                ValidationError("Выберите вид сделки.", code="invalid_choice"),
            )
        for quantity, _ in RANGE_LABELS:
            lower, upper = f"{quantity}_min", f"{quantity}_max"
            minimum, maximum = values.get(lower), values.get(upper)
            if minimum is not None and maximum is not None and minimum > maximum:
                for field in (lower, upper):
                    self.add_error(
                        field,
                        ValidationError(
                            "Значение «от» больше значения «до».", code="inverted_range"
                        ),
                    )
        return values

    def mark_missing_page(self) -> None:
        self.add_error(
            "page", ValidationError("Такой страницы уже нет.", code="out_of_range")
        )

    def field_errors(self) -> dict[str, list[FieldError]]:
        errors: dict[str, list[FieldError]] = {
            field: [
                {"code": error.code or "invalid_choice", "message": message}
                for error in errors
                for message in error.messages
            ]
            for field, errors in self.errors.as_data().items()
        }
        for field in self.query.keys() - self.fields.keys():
            errors[field] = [
                {"code": "unknown_parameter", "message": "Неизвестный параметр."}
            ]
        return errors

    def parameters(self) -> dict[str, str]:
        values: dict[str, str] = {
            "deal_type": self.cleaned_data.get("deal_type", DealType.SALE),
            "category": self.cleaned_data.get("category", "all"),
        }
        region = self.cleaned_data.get("region_id")
        if region is not None:
            values["region_id"] = str(region)
        for field in LOCATION_FIELDS:
            if value := self.cleaned_data.get(field):
                values[field] = value
        for quantity, _ in RANGE_LABELS:
            for suffix in ("min", "max"):
                field = f"{quantity}_{suffix}"
                value = self.cleaned_data.get(field)
                if value is not None:
                    values[field] = str(value)
        if sort := self.cleaned_data.get("sort"):
            values["sort"] = sort
        return values

    def canonical_query(self, *, page: int | str, from_catalog: bool = False) -> str:
        context = {"from": "catalog"} if from_catalog else {}
        return urlencode({**context, **self.parameters(), "page": str(page)})
