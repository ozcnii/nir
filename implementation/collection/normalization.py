"""Scalar normalization shared by collection adapters."""

import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import cast


class RejectedCard(ValueError):
    """A recognized card has invalid required or structural data."""


_NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?: [0-9]{3})+)(?:[.,][0-9]+)?")


def number(value: object) -> Decimal:
    if isinstance(value, str):
        value = value.strip().replace("\u00a0", " ").replace("\u202f", " ")
        if not _NUMBER.fullmatch(value):
            raise RejectedCard("invalid_number")
        value = value.replace(" ", "").replace(",", ".")
    elif type(value) not in (int, float, Decimal):
        raise RejectedCard("invalid_number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise RejectedCard("invalid_number") from error
    if not result.is_finite():
        raise RejectedCard("invalid_number")
    return result


def decimal_2(value: object, *, minimum: Decimal, maximum: Decimal) -> Decimal:
    result = number(value)
    if not minimum <= result < maximum:
        raise RejectedCard("number_out_of_range")
    rounded = result.quantize(Decimal("0.01"))
    if rounded != result:
        raise RejectedCard("invalid_precision")
    return rounded


def coordinate(value: object, limit: int) -> Decimal:
    result = number(value)
    if not -limit <= result <= limit:
        raise RejectedCard("coordinate_out_of_range")
    return result.quantize(Decimal("0.0000001"), rounding=ROUND_HALF_EVEN)


def optional_integer(value: object, *, minimum: int) -> int | None:
    if value is None:
        return None
    result = number(value)
    if not minimum <= result <= 32767 or result != result.to_integral_value():
        raise RejectedCard("invalid_integer")
    return int(result)


def as_object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}
