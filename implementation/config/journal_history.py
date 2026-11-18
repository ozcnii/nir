"""Bounded, read-only journal snapshots for the admin layer."""

import hashlib
import json
import os
import stat
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from collection.selection import InvalidSelection, parse
from config.control import MAX_MESSAGE, ControlError, decode
from config.run_events import LEVELS, SCHEMA_VERSION, STAGES, validate_message
from config.volume_owner import valid_identity

SEGMENT_LIMIT = 5 * 1024 * 1024
EVENT_FILES = ("events.jsonl.2", "events.jsonl.1", "events.jsonl")
STATE_FILE = "state.json"
TERMINAL_SEAL = "state.ack"
RUN_FILES = frozenset((*EVENT_FILES, STATE_FILE, TERMINAL_SEAL))
STATUSES = {"starting", "running", "stopping", "finished", "unknown"}
OUTCOMES = {"completed", "completed_with_errors", "stopped", "error"}

ReadKind = Literal[
    "ok",
    "empty",
    "partial",
    "corrupt",
    "unreadable",
    "changed",
    "missing",
    "snapshot_corrupt",
    "snapshot_unreadable",
]
Fingerprint = tuple[tuple[str, int, int, int], ...]


@dataclass(frozen=True)
class JournalView:
    kind: ReadKind
    snapshot: dict[str, object] | None = None
    events: tuple[dict[str, object], ...] = ()
    prefix_lost: bool = False
    identity: tuple[int, int] | None = None


@contextmanager
def directory(path: Path | str, parent: int | None = None) -> Iterator[int]:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def open_regular(parent: int, name: str, flags: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("journal path is not a regular file")
    return fd


def journal_time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid journal time")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("missing journal timezone")
    return result


def read_snapshot(run: int, identifier: str) -> dict[str, object]:
    with os.fdopen(open_regular(run, STATE_FILE, os.O_RDONLY), "rb") as stream:
        raw = stream.read(MAX_MESSAGE + 1)
    if len(raw) > MAX_MESSAGE:
        raise ControlError("state snapshot is oversized")
    record = decode(raw)
    state, outcome = record.get("status"), record.get("outcome")
    reason = record.get("reason")
    progress = record.get("progress")
    complete = record.get("cleanup_confirmed")
    if (
        set(record)
        != {
            "schema_version",
            "run_id",
            "instance_id",
            "status",
            "outcome",
            "reason",
            "started_at",
            "updated_at",
            "finished_at",
            "selection",
            "progress",
            "cleanup_confirmed",
        }
        or type(record["schema_version"]) is not int
        or record["schema_version"] != SCHEMA_VERSION
        or record["run_id"] != identifier
        or not valid_identity(record["instance_id"])
        or not isinstance(state, str)
        or state not in STATUSES
        or (
            outcome is not None
            and (not isinstance(outcome, str) or outcome not in OUTCOMES)
        )
        or not isinstance(reason, str)
        or len(reason) > 64
        or type(complete) is not bool
        or not isinstance(progress, dict)
        or not {"committed_writes"}
        <= set(progress)
        <= {"committed_writes", "incomplete"}
        or type(progress["committed_writes"]) is not int
        or not 0 <= progress["committed_writes"] < 2**63
        or ("incomplete" in progress and progress["incomplete"] is not True)
    ):
        raise ControlError("invalid state snapshot")
    if state == "finished":
        if not complete or outcome is None:
            raise ControlError("terminal cleanup is not confirmed")
        journal_time(record["finished_at"])
    elif complete or record["finished_at"] is not None:
        raise ControlError("invalid active snapshot")
    journal_time(record["started_at"])
    journal_time(record["updated_at"])
    parse(record["selection"])
    return record


def terminal_digest(snapshot: dict[str, object]) -> bytes:
    canonical = json.dumps(
        snapshot, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest().encode() + b"\n"


def terminal_confirmed(run: int, snapshot: dict[str, object]) -> bool:
    if snapshot["status"] != "finished":
        return False
    try:
        with os.fdopen(open_regular(run, TERMINAL_SEAL, os.O_RDONLY), "rb") as stream:
            return stream.read(66) == terminal_digest(snapshot)
    except OSError:
        return False


def file_fingerprint(run: int) -> Fingerprint:
    files = []
    for name in (*EVENT_FILES, STATE_FILE, TERMINAL_SEAL):
        try:
            info = os.stat(name, dir_fd=run, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise OSError("unsafe journal file")
        files.append((name, info.st_ino, info.st_size, info.st_mtime_ns))
    return tuple(files)


def read_events(
    run: int,
    names: tuple[str, ...],
    snapshot: dict[str, object],
    limit: int,
) -> JournalView:
    records: deque[dict[str, object]] = deque(maxlen=limit)
    previous = 0
    prefix_lost = False

    def result(kind: ReadKind) -> JournalView:
        return JournalView(kind, snapshot, tuple(records), prefix_lost)

    if not names or names[-1] != EVENT_FILES[-1]:
        return result("unreadable")
    try:
        for name in names:
            size = 0
            with os.fdopen(open_regular(run, name, os.O_RDONLY), "rb") as stream:
                while line := stream.readline(MAX_MESSAGE + 1):
                    size += len(line)
                    if size > SEGMENT_LIMIT or len(line) > MAX_MESSAGE:
                        return result("corrupt")
                    if not line.endswith(b"\n"):
                        return result("partial" if name == names[-1] else "corrupt")
                    event = decode(line[:-1])
                    journal_time(event.get("time"))
                    level, stage, message = (
                        event.get("level"),
                        event.get("stage"),
                        event.get("message"),
                    )
                    if (
                        not isinstance(level, str)
                        or level not in LEVELS
                        or not isinstance(stage, str)
                        or stage not in STAGES
                        or not isinstance(message, str)
                        or message != (event.get("reason") or event.get("event"))
                    ):
                        return result("corrupt")
                    seq = event.get("seq")
                    if type(seq) is not int or not 1 <= seq < 2**63:
                        return result("corrupt")
                    if previous and seq != previous + 1:
                        return result("corrupt")
                    if not previous:
                        prefix_lost = seq != 1
                    wire = dict(event)
                    del wire["time"], wire["message"]
                    if event.get("event") != "event":
                        del wire["level"], wire["stage"]
                    validate_message(
                        wire,
                        cast(str, snapshot["instance_id"]),
                        cast(str, snapshot["run_id"]),
                        seq,
                    )
                    records.append(event)
                    previous = seq
    except (ControlError, ValueError):
        return result("corrupt")
    except OSError:
        return result("unreadable")
    return result("ok" if records else "empty")


def read_run(root: Path, identifier: str, *, limit: int = 100) -> JournalView:
    if not valid_identity(identifier) or not 1 <= limit <= 512:
        raise ValueError("invalid journal request")
    try:
        with directory(root) as parent, directory(identifier, parent) as run:
            before = file_fingerprint(run)
            try:
                snapshot = read_snapshot(run, identifier)
            except OSError:
                view = JournalView("snapshot_unreadable")
            except (ControlError, ValueError, InvalidSelection):
                view = JournalView("snapshot_corrupt")
            else:
                names = tuple(name for name, *_ in before if name in EVENT_FILES)
                view = read_events(run, names, snapshot, limit)
                if snapshot["status"] == "finished" and not terminal_confirmed(
                    run, snapshot
                ):
                    progress = cast(dict[str, object], snapshot["progress"])
                    view = replace(
                        view,
                        kind="partial" if view.kind in ("ok", "empty") else view.kind,
                        snapshot={
                            **snapshot,
                            "status": "unknown",
                            "outcome": None,
                            "reason": "cleanup_not_confirmed",
                            "finished_at": None,
                            "progress": {**progress, "incomplete": True},
                            "cleanup_confirmed": False,
                        },
                    )
            try:
                after = file_fingerprint(run)
                current = os.stat(identifier, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return JournalView("changed")
            original = os.fstat(run)
            if before != after or (original.st_dev, original.st_ino) != (
                current.st_dev,
                current.st_ino,
            ):
                return JournalView("changed")
            return replace(view, identity=(original.st_dev, original.st_ino))
    except FileNotFoundError:
        return JournalView("missing")
    except OSError:
        return JournalView("unreadable")
