"""Delete reference entries only when collection cleanup is confirmed."""

from collections.abc import Sequence
from pathlib import Path

from django.db import transaction

from catalog.models import PropertyType, Region
from runtime.control import CONTROL_PATH, ControlError, request
from runtime.postgres_locks import try_delete_lock


class ReferenceDeleteBlocked(RuntimeError):
    pass


def delete_references(
    reference: str,
    ids: Sequence[int],
    *,
    socket_path: Path = CONTROL_PATH,
) -> int:
    if reference not in ("regions", "property_types"):
        raise ValueError("unsupported reference dictionary")
    with transaction.atomic():
        if not try_delete_lock():
            raise ReferenceDeleteBlocked("collection or reference deletion is active")
        try:
            status = request({"action": "status"}, socket_path)
        except ControlError as error:
            raise ReferenceDeleteBlocked("collection status is unavailable") from error
        if (
            status.get("ok") is not True
            or status.get("state") not in ("idle", "finished")
            or status.get("cleanup_confirmed") is not True
        ):
            raise ReferenceDeleteBlocked("collection cleanup has not been confirmed")
        records = (
            Region.objects.filter(pk__in=ids)
            if reference == "regions"
            else PropertyType.objects.filter(pk__in=ids)
        )
        deleted, _ = records.delete()
        return deleted
