"""Narrow runner-to-launcher event schema and the private PIPE reader."""

import os
import re
import select
import threading
from typing import IO, cast

from catalog.reference_data_v1 import SOURCE_CODES
from config.control import MAX_MESSAGE, ControlError, decode, encode
from config.journal import JournalWriter, timestamp

RUN_ID_ENV = "NIR_RUN_ID"
INSTANCE_ID_ENV = "NIR_INSTANCE_ID"
SCHEMA_VERSION = 1
REASONS = {
    "",
    "completed",
    "completed_with_errors",
    "stopped",
    "invalid_selection",
    "unsupported_direction",
    "unsupported_or_disabled_source",
    "unsupported_or_missing_region",
    "unsupported_or_missing_property_type",
    "unsupported_deal_type",
    "lock_busy",
    "lock_lost",
    "database_error",
    "run_failed",
    "runner_admission_missing",
    "browser_initialization_failed",
    "browser_operation_failed",
    "browser_disconnected",
    "browser_children_unconfirmed",
    "browser_cleanup_unconfirmed",
    "navigation_timeout",
    "network_error",
    "http_server_error",
}
STAGES = {"startup", "listing", "card", "write", "cleanup"}
LEVELS = {"info", "warning", "error"}
PUBLICATION_KEY = re.compile(
    r"(?:[1-9][0-9]{0,31}|/detail/[1-9][0-9]{0,31}|"
    r"/card/(?:[a-z]+__[a-z]+__[1-9][0-9]{0,31}|"
    r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}))"
)


def validate_event(
    raw: bytes, instance_id: str, run_id: str, sequence: int
) -> dict[str, object]:
    message = decode(raw)
    event = message.get("event")
    reason = message.get("reason")
    core = {"event", "reason", "schema_version", "run_id", "instance_id", "seq"}
    if (
        type(message.get("schema_version")) is not int
        or message["schema_version"] != SCHEMA_VERSION
        or message.get("run_id") != run_id
        or message.get("instance_id") != instance_id
        or type(message.get("seq")) is not int
        or message["seq"] != sequence
        or not isinstance(reason, str)
        or reason not in REASONS
    ):
        raise ControlError("invalid runner event")
    if event == "progress":
        count = message.get("committed_writes")
        if (
            set(message) != core | {"committed_writes"}
            or type(count) is not int
            or not 0 < count < 2**63
            or reason
        ):
            raise ControlError("invalid runner progress")
    elif event == "event":
        stage, level = message.get("stage"), message.get("level")
        source, key = message.get("source"), message.get("publication_key")
        if (
            not core | {"stage", "level"}
            <= set(message)
            <= core | {"stage", "level", "source", "publication_key"}
            or not isinstance(stage, str)
            or stage not in STAGES
            or not isinstance(level, str)
            or level not in LEVELS
            or (source is not None and source not in SOURCE_CODES)
            or (
                key is not None
                and (
                    source is None
                    or not isinstance(key, str)
                    or len(key) > 96
                    or PUBLICATION_KEY.fullmatch(key) is None
                )
            )
            or not reason
        ):
            raise ControlError("invalid runner diagnostic")
    elif (
        event not in ("armed", "ready", "finished", "fault")
        or set(message) != core
        or ((event in ("armed", "ready")) != (not reason))
    ):
        raise ControlError("invalid runner lifecycle event")
    return message


def journal_record(message: dict[str, object]) -> dict[str, object]:
    event = message["event"]
    reason = message["reason"]
    record = {
        **message,
        "time": timestamp(),
        "stage": message.get(
            "stage",
            "write"
            if event == "progress"
            else "startup"
            if event in ("armed", "ready")
            else "cleanup",
        ),
        "level": message.get("level", "error" if event == "fault" else "info"),
        "message": str(reason or event),
    }
    # Enforce the limit on the serialized entry, not only the input frame.
    encode(record)
    return record


class RunnerReader:
    def __init__(
        self,
        stream: IO[bytes],
        journal: JournalWriter,
        run_id: str,
    ) -> None:
        self._state = threading.Lock()
        self._control: list[tuple[str, str]] = []
        self._progress = 0
        self._invalid = False
        self._drained = False
        self._peak = 0
        self._reaped = threading.Event()
        self._closing = threading.Event()
        self._thread = threading.Thread(
            target=self._read,
            args=(stream, journal, run_id),
            daemon=True,
            name="runner-pipe",
        )
        self._thread.start()

    def collect(self) -> tuple[list[tuple[str, str]], int, bool, bool]:
        with self._state:
            events, self._control = self._control, []
            return events, self._progress, self._invalid, self._drained

    @property
    def peak(self) -> int:
        return self._peak

    def reaped(self) -> None:
        self._reaped.set()

    def close(self) -> None:
        self._closing.set()

    def _read(self, stream: IO[bytes], journal: JournalWriter, run_id: str) -> None:
        buffer = bytearray()
        sequence = 0
        progress = 0
        seen: set[str] = set()
        terminal = False
        normal_end = False
        try:
            with stream:
                fd = stream.fileno()
                while not self._closing.is_set():
                    if not select.select([fd], [], [], 0.05)[0]:
                        if self._reaped.is_set():
                            break
                        continue
                    chunk = os.read(fd, MAX_MESSAGE - len(buffer))
                    if not chunk:
                        break
                    if self._invalid:
                        if self._reaped.is_set():
                            break
                        continue
                    buffer.extend(chunk)
                    self._peak = max(self._peak, len(buffer))
                    try:
                        if terminal:
                            raise ControlError("bytes after terminal event")
                        while (end := buffer.find(b"\n")) >= 0:
                            line = bytes(buffer[:end])
                            del buffer[: end + 1]
                            sequence += 1
                            message = validate_event(
                                line, journal.instance_id, run_id, sequence
                            )
                            event = cast(str, message["event"])
                            if event not in ("event", "progress"):
                                if event in seen:
                                    raise ControlError("duplicate lifecycle event")
                                seen.add(event)
                            if event == "ready" and "armed" not in seen:
                                raise ControlError("ready before armed")
                            if event == "progress":
                                count = cast(int, message["committed_writes"])
                                if "ready" not in seen or count <= progress:
                                    raise ControlError("invalid progress sequence")
                                progress = count
                            ticket = journal.submit(
                                run_id, journal_record(message), event=True
                            )
                            with self._state:
                                if event == "progress":
                                    if ticket is not None:
                                        self._progress = progress
                                elif event != "event":
                                    self._control.append(
                                        (event, cast(str, message["reason"]))
                                    )
                            if event in ("finished", "fault"):
                                terminal = True
                                if buffer:
                                    raise ControlError("bytes after terminal event")
                        if len(buffer) == MAX_MESSAGE:
                            raise ControlError("oversized runner event")
                    except ControlError:
                        with self._state:
                            self._invalid = True
                        buffer.clear()
            normal_end = True
        except Exception:
            # Unhandled reader failure cannot acknowledge cleanup or expose payload.
            pass
        finally:
            with self._state:
                self._invalid |= not normal_end or bool(buffer)
                self._drained = True
