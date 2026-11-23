"""Update the catalogue's visitor lists in the existing Django session."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import cast
from urllib.parse import urlsplit

from django import forms
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import transaction
from django.http import HttpRequest
from django.urls import Resolver404, resolve
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from app.catalog.catalog_filters import BIGINT_MAX
from app.catalog.catalog_labels import DEAL_LABELS

FAVORITES = "catalog_shortlist"
COMPARISON = "catalog_comparison"


class SelectionForm(forms.Form):
    deal = forms.ChoiceField(choices=DEAL_LABELS.items())
    property_id = forms.IntegerField(min_value=1, max_value=BIGINT_MAX)
    action = forms.ChoiceField(choices=(("save", "Добавить"), ("remove", "Убрать")))


def selection(request: HttpRequest, key: str) -> dict[str, list[int]]:
    return cast(dict[str, list[int]], request.session.get(key, {}))


def edit_selection(
    request: HttpRequest, key: str
) -> AbstractContextManager[dict[str, list[int]]]:
    return edit_mapping(request, key, {})


@contextmanager
def edit_mapping[T](
    request: HttpRequest, key: str, default: dict[str, T]
) -> Iterator[dict[str, T]]:
    with transaction.atomic():
        store = request.session
        session_key = store.session_key
        if (
            session_key
            and Session.objects.select_for_update()
            .filter(pk=session_key, expire_date__gt=timezone.now())
            .exists()
        ):
            # Read again after obtaining the row lock to keep writes from other tabs.
            store = SessionStore(session_key)
        current = cast(dict[str, T], store.get(key, default))
        original = current.copy()
        yield current
        if current != original:
            store[key] = current
            if store is not request.session:
                store.save()


def local_route(location: str, names: set[str]) -> str | None:
    if not location.startswith("/") or not url_has_allowed_host_and_scheme(
        location, allowed_hosts=set()
    ):
        return None
    try:
        name = resolve(urlsplit(location).path).url_name
    except Resolver404:
        return None
    return location if name in names else None


def comparison_navigation(request: HttpRequest) -> dict[str, int]:
    comparing = selection(request, COMPARISON)
    match = request.resolver_match
    if (
        request.method == "GET"
        and match is not None
        and match.url_name
        in {"catalog", "map_search", "property_detail", "favorites", "compare"}
        and not request.session.session_key
    ):
        # Establish the guest session when rendering the controls, before either tab can save.
        request.session.setdefault(COMPARISON, {})
    return {"comparison_count": sum(map(len, comparing.values()))}
