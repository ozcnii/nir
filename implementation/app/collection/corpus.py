"""Persist the first research observation for each source and publication key."""

import errno
import json
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit

from django.core.serializers.json import DjangoJSONEncoder

from app.catalog.contracts import ActiveSnapshot
from app.collection.listing_page import SearchPage
from app.collection.observations import Observation, public_text


class Corpus:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.saved = 0

    def _path(self, source: str, key: str) -> Path:
        return self.directory / source / quote(key, safe="")

    def contains(self, source: str, key: str) -> bool:
        return self._path(source, key).is_dir()

    def save(
        self,
        observation: Observation,
        *,
        route: str | None = None,
        page: SearchPage | None = None,
        in_scope: Callable[[ActiveSnapshot], bool] | None = None,
    ) -> None:
        result = observation.result
        if (
            not isinstance(result, str)
            and in_scope is not None
            and not in_scope(result)
        ):
            return
        source = observation.key["source_code"]
        key = observation.key["external_id"]
        destination = self._path(source, key)
        if destination.is_dir():
            return
        identity = {
            **observation.key,
            "observed_at": observation.key["observed_at"].isoformat(),
            "url": urlsplit(observation.url)._replace(query="", fragment="").geturl(),
        }
        raw = {
            **identity,
            "version": 1,
            "listing_route": route,
            "listing_page": page.number if page else None,
            "listing_scope": page.filters if page else None,
            "published_fields": observation.published,
        }
        normalized: dict[str, object] = (
            {"state": "rejected", "reason": result}
            if isinstance(result, str)
            else dict(result)
        )
        for field in ("description", "address"):
            if isinstance(value := normalized.get(field), str):
                normalized[field] = public_text(value)
        normalized.update(identity)

        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        with TemporaryDirectory(prefix=".card-", dir=destination.parent) as staging:
            pending = Path(staging)
            for name, data in (("raw.json", raw), ("normalized.json", normalized)):
                (pending / name).write_text(
                    json.dumps(data, ensure_ascii=False, cls=DjangoJSONEncoder) + "\n",
                    encoding="utf-8",
                )
            try:
                pending.rename(destination)
            except OSError as error:
                if (
                    error.errno not in (errno.EEXIST, errno.ENOTEMPTY)
                    or not destination.is_dir()
                ):
                    raise
            else:
                self.saved += 1
