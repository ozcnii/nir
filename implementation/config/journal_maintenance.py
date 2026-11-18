"""Journal rotation, retention and restart repair, called by its single writer."""

import os
from collections.abc import Callable, Generator, Iterator
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import cast

from collection.selection import InvalidSelection
from config.control import ControlError, encode
from config.journal_history import (
    EVENT_FILES,
    RUN_FILES,
    SEGMENT_LIMIT,
    directory,
    file_fingerprint,
    journal_time,
    open_regular,
    read_run,
    read_snapshot,
    terminal_confirmed,
)
from config.run_events import timestamp
from config.volume_owner import valid_identity

KEEP_FINISHED = 10
SnapshotWriter = Callable[[int, dict[str, object]], None]


def append_event(run: int, record: dict[str, object]) -> None:
    data = encode(record)
    oldest, previous, active = EVENT_FILES
    sizes = {name: size for name, _, size, _ in file_fingerprint(run)}
    if active not in sizes:
        raise OSError("active journal segment is missing")
    rotated = sizes[active] + len(data) > SEGMENT_LIMIT
    if rotated:
        if previous in sizes:
            os.replace(previous, oldest, src_dir_fd=run, dst_dir_fd=run)
        os.replace(active, previous, src_dir_fd=run, dst_dir_fd=run)
    flags = os.O_WRONLY | os.O_APPEND
    if rotated:
        flags |= os.O_CREAT | os.O_EXCL
    with os.fdopen(open_regular(run, active, flags), "ab") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if rotated:
        os.fsync(run)


def snapshots(parent: int) -> Iterator[tuple[str, int, dict[str, object]] | None]:
    with os.scandir(parent) as entries:
        for entry in entries:
            if not valid_identity(entry.name) or not entry.is_dir(
                follow_symlinks=False
            ):
                yield None
                continue
            try:
                with directory(entry.name, parent) as run:
                    yield entry.name, run, read_snapshot(run, entry.name)
            except (OSError, ControlError, ValueError, InvalidSelection):
                # Preserve an unreadable, unsupported, or corrupt history as-is.
                yield None


def recover_runs(root: Path, write: SnapshotWriter) -> None:
    with directory(root.parent) as volume:
        with suppress(FileExistsError):
            os.mkdir(root.name, 0o700, dir_fd=volume)
        os.fsync(volume)
        with directory(root.name, volume) as parent:
            for item in snapshots(parent):
                if item is None:
                    continue
                _, run, snapshot = item
                if terminal_confirmed(run, snapshot):
                    continue
                now = timestamp()
                progress = cast(dict[str, object], snapshot["progress"])
                write(
                    run,
                    {
                        **snapshot,
                        "status": "finished",
                        "outcome": "error",
                        "reason": "app_restarted",
                        "updated_at": now,
                        "finished_at": now,
                        "progress": {**progress, "incomplete": True},
                        "cleanup_confirmed": True,
                    },
                )


def retire(root: Path, identifier: str, identity: tuple[int, int]) -> None:
    # The rename makes the whole directory disappear from reader selections.
    # Unlink only the fixed journal entries inside the already-open directory.
    retired = f".retiring-{identifier}"
    with directory(root) as parent, directory(identifier, parent) as run:
        info = os.fstat(run)
        if (info.st_dev, info.st_ino) != identity or not set(
            os.listdir(run)
        ) <= RUN_FILES:
            return
        files = file_fingerprint(run)
        try:
            os.stat(retired, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return
        os.rename(identifier, retired, src_dir_fd=parent, dst_dir_fd=parent)
        withdrawn = os.stat(retired, dir_fd=parent, follow_symlinks=False)
        if (withdrawn.st_dev, withdrawn.st_ino) != identity:
            raise OSError("withdrawn journal changed identity")
        os.fsync(parent)
        for name, *_ in files:
            os.unlink(name, dir_fd=run)
        os.rmdir(retired, dir_fd=parent)
        os.fsync(parent)


def retention_steps(root: Path, current: str | None = None) -> Generator[None]:
    candidates: list[tuple[datetime, str]] = []
    with directory(root) as parent:
        for item in snapshots(parent):
            if item is not None:
                identifier, run, snapshot = item
                if terminal_confirmed(run, snapshot):
                    candidates.append(
                        (journal_time(snapshot["finished_at"]), identifier)
                    )
            yield
    if len(candidates) <= KEEP_FINISHED:
        return
    retained = 0
    for _, identifier in sorted(candidates, reverse=True):
        view = read_run(root, identifier, limit=1)
        if view.kind in ("ok", "empty") and view.identity is not None:
            retained += 1
            if retained > KEEP_FINISHED and identifier != current:
                # An independent cleanup may have withdrawn the candidate meanwhile.
                with suppress(FileNotFoundError):
                    retire(root, identifier, view.identity)
        yield
