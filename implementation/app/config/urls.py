from django.contrib import admin
from django.urls import path, re_path
from django.views.decorators.http import require_safe
from django.views.generic import RedirectView

from app.catalog import comparison_view, json_api, map_search, market, shortlist, views

handler500 = json_api.server_error

urlpatterns = [
    path(
        "",
        require_safe(RedirectView.as_view(pattern_name="catalog", query_string=True)),
    ),
    path("catalog/", views.catalog, name="catalog"),
    path("catalog/locations/", views.location_options, name="location_options"),
    path("market/", market.market, name="market"),
    path("map/", map_search.map_search, name="map_search"),
    path("favorites/", shortlist.favorites, name="favorites"),
    path("compare/", comparison_view.compare, name="compare"),
    path(
        "properties/<int:property_id>/", views.property_detail, name="property_detail"
    ),
    path("api/properties/", json_api.property_list, name="api_property_list"),
    path(
        "api/properties/<str:property_id>/",
        json_api.property_detail,
        name="api_property_detail",
    ),
    re_path(r"^api/", json_api.resource_not_found),
    path("admin/", admin.site.urls),
]
