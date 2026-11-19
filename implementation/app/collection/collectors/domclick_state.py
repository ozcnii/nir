"""Read the observed inline state serializations without evaluating site code."""

import json
import re
from decimal import Decimal
from typing import cast

from app.collection.collectors.nuxt_state import UNDEFINED, resolve_reference
from app.collection.normalization import as_object

SSR_ASSIGNMENT = re.compile(r"^\s*window\.__SSR_STATE__\s*=\s*")
SSR_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\bundefined\b')


def ssr_state(scripts: list[str]) -> dict[str, object]:
    bodies = [
        script[marker.end() :]
        for script in scripts
        if (marker := SSR_ASSIGNMENT.match(script))
    ]
    if len(bodies) != 1:
        return {}

    # NaN is only an internal JSON-decoder token here. The sentinel lets fields
    # distinguish a missing JavaScript value from an explicit JSON null.
    def constant(token: str) -> object:
        if token == "NaN":
            return UNDEFINED
        raise ValueError("unexpected SSR constant")

    try:
        value, _ = json.JSONDecoder(
            parse_float=Decimal, parse_constant=constant
        ).raw_decode(
            SSR_TOKEN.sub(
                lambda match: "NaN" if match[0] == "undefined" else match[0],
                bodies[0],
            )
        )
    except (ValueError, RecursionError):
        return {}
    return as_object(value)


NUXT_WRAPPERS = ("ShallowReactive", "Reactive", "Ref", "ShallowRef")


class NuxtState:
    """Resolve only the card fields the adapter needs from Nuxt's index table."""

    def __init__(self, entries: list[object]) -> None:
        self.entries = entries

    def resolve(self, index: object) -> object:
        value = resolve_reference(self.entries, index, NUXT_WRAPPERS)
        if isinstance(value, list) and value and isinstance(value[0], str):
            raise ValueError("invalid Nuxt reference")
        return value

    def field(self, record: object, name: str) -> object:
        fields = as_object(record)
        return self.resolve(fields[name]) if name in fields else UNDEFINED


def nuxt_card(
    scripts: list[str], external_id: str
) -> tuple[NuxtState, dict[str, object]] | None:
    if len(scripts) != 1:
        return None
    try:
        entries = json.loads(scripts[0], parse_float=Decimal)
        if not isinstance(entries, list):
            return None
        state = NuxtState(cast(list[object], entries))
        data = state.field(state.resolve(0), "data")
        detail = as_object(state.field(data, f"detail:{external_id}"))
    except (ValueError, RecursionError):
        return None
    return (state, detail) if detail else None
