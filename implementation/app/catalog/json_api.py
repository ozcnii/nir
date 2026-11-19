"""Serve the public catalog and property detail JSON contract."""

from datetime import UTC, datetime
from decimal import Decimal

from django.core.paginator import EmptyPage
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import reverse
from django.views import defaults
from django.views.decorators.csrf import csrf_exempt

from app.catalog.catalog_filters import BIGINT_MAX, CatalogFilters, FieldError
from app.catalog.catalog_queries import PAGE_SIZE, read_catalog
from app.catalog.models import Ad, PriceHistory, Property
from app.catalog.property_details import property_details


def _decimal(value: Decimal | None, places: int) -> str | None:
    return format(value, f".{places}f") if value is not None else None


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _price(price: PriceHistory) -> dict[str, object]:
    return {
        "price_rub": format(price.price_rub, ".2f"),
        "price_kind": price.price_kind,
        "observed_at": _timestamp(price.observed_at),
    }


def _ad(ad: Ad, latest: PriceHistory | None) -> dict[str, object]:
    return {
        "id": ad.pk,
        "source_code": ad.source_id,
        "external_id": ad.external_id,
        "url": ad.url,
        "deal_type": ad.deal_type,
        "market_segment": ad.market_segment,
        "is_active": ad.is_active,
        "observed_at": _timestamp(ad.observed_at),
        "photos": ad.photos,
        "photos_observed_at": (
            _timestamp(ad.photos_observed_at) if ad.photos_observed_at else None
        ),
        "latest_price": _price(latest) if latest is not None else None,
    }


def _property(item: Property, ads: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": item.pk,
        "region": {"id": item.region_id, "name": item.region.name},
        "property_type": {"id": item.property_type_id, "name": item.property_type.name},
        "address": item.address,
        "latitude": _decimal(item.latitude, 7),
        "longitude": _decimal(item.longitude, 7),
        "area_m2": _decimal(item.area_m2, 2),
        "rooms": item.rooms,
        "floor": item.floor,
        "floors_total": item.floors_total,
        "land_area_m2": _decimal(item.land_area_m2, 2),
        "ads": ads,
    }


def _error(
    status: int,
    code: str,
    message: str,
    fields: dict[str, list[FieldError]] | None = None,
) -> JsonResponse:
    return JsonResponse(
        {"error": {"code": code, "message": message, "fields": fields or {}}},
        status=status,
    )


def _invalid_query(fields: dict[str, list[FieldError]]) -> JsonResponse:
    return _error(400, "invalid_query", "Исправьте параметры запроса.", fields)


def _method_not_allowed() -> JsonResponse:
    response = _error(405, "method_not_allowed", "Метод не поддерживается.")
    response.headers["Allow"] = "GET"
    return response


# Public read-only API routes return 405 before CSRF can reject an unsupported method.
@csrf_exempt
def property_list(request: HttpRequest) -> JsonResponse:
    if request.method != "GET":
        return _method_not_allowed()
    filters = CatalogFilters(request.GET)
    if errors := filters.field_errors():
        return _invalid_query(errors)
    try:
        page, results = read_catalog(filters)
    except EmptyPage:
        filters.mark_missing_page()
        return _invalid_query(filters.field_errors())

    path = reverse("api_property_list")
    return JsonResponse(
        {
            "count": page.paginator.count,
            "page": page.number,
            "page_size": PAGE_SIZE,
            "next": (
                f"{path}?{filters.canonical_query(page=page.next_page_number())}"
                if page.has_next()
                else None
            ),
            "previous": (
                f"{path}?{filters.canonical_query(page=page.previous_page_number())}"
                if page.has_previous()
                else None
            ),
            "results": [
                _property(
                    result.property,
                    [_ad(offer.ad, offer.latest_price) for offer in result.offers],
                )
                for result in results
            ],
        }
    )


@csrf_exempt
def property_detail(request: HttpRequest, property_id: str) -> JsonResponse:
    if request.method != "GET":
        return _method_not_allowed()
    normalized_id = property_id.lstrip("0")
    if (
        not normalized_id.isascii()
        or not normalized_id.isdecimal()
        or len(normalized_id) > len(str(BIGINT_MAX))
        or (identifier := int(normalized_id)) > BIGINT_MAX
    ):
        return _error(404, "not_found", "Объект не найден.")
    if request.GET:
        return _invalid_query(
            {
                field: [
                    {"code": "unknown_parameter", "message": "Неизвестный параметр."}
                ]
                for field in request.GET
            }
        )

    details = property_details(identifier)
    if details is None:
        return _error(404, "not_found", "Объект не найден.")
    return JsonResponse(
        _property(
            details.property,
            [
                {
                    **_ad(offer.ad, offer.latest_price),
                    "description": offer.ad.description,
                    "price_history": [_price(price) for price in offer.price_history],
                }
                for offer in details.offers
            ],
        )
    )


@csrf_exempt
def resource_not_found(request: HttpRequest) -> JsonResponse:
    return _error(404, "not_found", "Ресурс не найден.")


def server_error(request: HttpRequest) -> HttpResponse:
    if request.path_info.startswith("/api/"):
        return _error(500, "internal_error", "Не удалось загрузить данные.")
    return defaults.server_error(request)
