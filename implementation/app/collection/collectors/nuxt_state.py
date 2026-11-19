"""Resolve references in the indexed state of partner cards."""

from typing import cast

UNDEFINED = object()


def resolve_reference(
    entries: list[object],
    ref: object,
    wrappers: tuple[str, ...],
    *,
    max_hops: int | None = None,
) -> object:
    seen: set[int] = set()
    while type(ref) is int:
        if ref == -1:
            return UNDEFINED
        if (
            not 0 <= ref < len(entries)
            or ref in seen
            or (max_hops is not None and len(seen) == max_hops)
        ):
            break
        seen.add(ref)
        value = entries[ref]
        if not isinstance(value, list) or not value:
            return value
        items = cast(list[object], value)
        if items[0] not in wrappers:
            return items
        if len(items) != 2:
            break
        ref = items[1]
    raise ValueError("invalid Nuxt reference")
