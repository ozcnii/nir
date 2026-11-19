from django.contrib import admin
from django.urls import path
from django.views.decorators.http import require_safe
from django.views.generic import RedirectView

from app.catalog import views

urlpatterns = [
    path(
        "",
        require_safe(RedirectView.as_view(pattern_name="catalog", query_string=True)),
    ),
    path("catalog/", views.catalog, name="catalog"),
    path(
        "properties/<int:property_id>/", views.property_detail, name="property_detail"
    ),
    path("admin/", admin.site.urls),
]
