"""The optional research observation emitted by a verified card adapter."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from html import unescape
from typing import cast

from django.utils.html import strip_tags

from app.catalog.contracts import ActiveSnapshot, SnapshotKey, is_photo_url
from app.collection.normalization import RejectedCard, as_object

_CONTACT = re.compile(
    r"(?i)(?:https?://|www\.|(?:t\.me|wa\.me|vk\.com)/)\S+"
    r"|[\w.+-]+@[\w.-]+\.[\w-]+|(?<!\w)@[\w.-]+"
    r"|(?:telegram|телеграм|whatsapp|ватсап)[ \t]*[:=][ \t]*@?[\w.-]+"
    r"|(?<!\d)(?:(?:\+?7|8)[\s().-]*\d{3}|9[\s().-]*\d{2})"
    r"(?:[\s().-]*\d){7}(?!\d)"
)


def public_text(value: str) -> str:
    return _CONTACT.sub("[контакт удалён]", strip_tags(unescape(value)))


def public_fields(
    value: object, *names: str, text: tuple[str, ...] = ()
) -> dict[str, object]:
    data = as_object(value)
    result: dict[str, object] = {}
    for name in (*names, *text):
        if name not in data:
            continue
        item = data[name]
        if item is None or isinstance(item, (str, int, Decimal)):
            result[name] = (
                public_text(item) if name in text and isinstance(item, str) else item
            )
    return result


def rows(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def photo_links(value: object) -> list[str]:
    links = [
        f"https:{url}" if url.startswith("//") else url
        for url in rows(value)
        if isinstance(url, str)
    ]
    return [url for url in links if is_photo_url(url)]


@dataclass(frozen=True)
class Observation:
    key: SnapshotKey
    url: str
    published: dict[str, object]
    result: ActiveSnapshot | str


type Observe = Callable[[Observation], None]


def observe_publication(
    observe: Observe | None,
    key: SnapshotKey,
    url: str,
    published: Callable[[], dict[str, object]],
    normalize: Callable[[], ActiveSnapshot],
) -> ActiveSnapshot:
    if observe is None:
        return normalize()
    try:
        snapshot = normalize()
    except RejectedCard as error:
        if str(error) not in {"hotel_accommodation", "hotel_or_ambiguous_room"}:
            observe(Observation(key, url, published(), str(error)))
        raise
    observe(Observation(key, url, published(), snapshot))
    return snapshot
