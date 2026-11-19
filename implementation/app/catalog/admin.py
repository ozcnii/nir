from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import ProtectedError, QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse

from app.catalog.locks import CollectionBusy
from app.catalog.models import PropertyType, Region, Source
from app.catalog.reference_defaults import SOURCE_CODES, SOURCE_LABELS
from app.catalog.references import delete_references

if TYPE_CHECKING:
    SourceAdminBase = admin.ModelAdmin[Source]
    ReferenceAdminBase = admin.ModelAdmin[Region | PropertyType]
else:
    SourceAdminBase = ReferenceAdminBase = admin.ModelAdmin


@admin.register(Source)
class SourceAdmin(SourceAdminBase):
    list_display = ("source_label", "enabled")
    fields = ("source_label", "enabled")
    readonly_fields = ("source_label",)

    @admin.display(description="Площадка")
    def source_label(self, source: Source) -> str:
        return SOURCE_LABELS[source.code]

    def get_queryset(self, request: HttpRequest) -> QuerySet[Source]:
        return (
            super().get_queryset(request).filter(code__in=SOURCE_CODES).order_by("code")
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Source | None = None
    ) -> bool:
        return False

    def save_model(
        self,
        request: HttpRequest,
        obj: Source,
        form: ModelForm[Source],
        change: bool,
    ) -> None:
        obj.save(update_fields=("enabled",))


@admin.register(Region, PropertyType)
class ReferenceAdmin(ReferenceAdminBase):
    fields = ("name",)
    list_display = ("name",)
    ordering = ("name",)

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, object] | None = None
    ) -> HttpResponse:
        if (
            request.method == "POST"
            and "delete_selected" in request.POST.getlist("action")
            and not self.has_delete_permission(request)
        ):
            raise PermissionDenied
        return super().changelist_view(request, extra_context)

    def delete_model(self, request: HttpRequest, obj: Region | PropertyType) -> None:
        delete_references(self.opts.db_table, [obj.pk])

    def delete_queryset(
        self, request: HttpRequest, queryset: QuerySet[Region | PropertyType]
    ) -> None:
        delete_references(
            self.opts.db_table, list(queryset.values_list("pk", flat=True))
        )

    def delete_view(
        self,
        request: HttpRequest,
        object_id: str,
        extra_context: dict[str, object] | None = None,
    ) -> HttpResponse:
        try:
            return super().delete_view(request, object_id, extra_context)
        except (CollectionBusy, ProtectedError) as error:
            return self.delete_refused(request, error)

    def response_action(
        self, request: HttpRequest, queryset: QuerySet[Region | PropertyType]
    ) -> HttpResponse | None:
        try:
            # Django logs bulk deletions before calling delete_queryset.
            with transaction.atomic():
                return super().response_action(request, queryset)
        except (CollectionBusy, ProtectedError) as error:
            return self.delete_refused(request, error)

    def delete_refused(
        self, request: HttpRequest, error: CollectionBusy | ProtectedError
    ) -> HttpResponseRedirect:
        message = (
            "Удаление недоступно, пока выполняется сбор."
            if isinstance(error, CollectionBusy)
            else "Запись используется в каталоге и не может быть удалена."
        )
        self.message_user(request, message, messages.ERROR)
        route = (
            f"admin:{self.opts.app_label}_{self.opts.model_name}_changelist"
            if self.has_view_or_change_permission(request)
            else "admin:index"
        )
        return HttpResponseRedirect(reverse(route, current_app=self.admin_site.name))
