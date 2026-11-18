"""Single owner of the launcher's on-volume journal writes."""

import json
import os
import queue
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

JOURNAL_ROOT = Path("/var/lib/app/runs")
QUEUE_LIMIT = 256
ACK_SECONDS = 5.0


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class Ticket:
    instance_id: str
    run_id: str
    operation: int
    deadline: float
    committed: bool = False


@dataclass(frozen=True)
class Job:
    ticket: Ticket
    record: dict[str, object]
    event: bool
    initial: bool


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_snapshot(path: Path, record: dict[str, object]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


class JournalWriter:
    def __init__(self, root: Path, instance_id: str) -> None:
        self.root = root
        self.instance_id = instance_id
        self._queue: queue.Queue[Job] = queue.Queue(maxsize=QUEUE_LIMIT)
        self._state = threading.Lock()
        self._pending: dict[int, Ticket] = {}
        self._operation = 0
        self._fault: str | None = None
        self._lost = 0
        self._closing = threading.Event()
        self._fault_notice = threading.Event()
        self._reporter = threading.Thread(
            target=self._report_fault, daemon=True, name="journal-fault-notice"
        )
        self._thread = threading.Thread(
            target=self._write, daemon=True, name="run-journal"
        )
        self._reporter.start()
        self._thread.start()

    def _fail(self, reason: str) -> None:
        if self._fault is None:
            self._fault = reason
            self._fault_notice.set()

    def _report_fault(self) -> None:
        self._fault_notice.wait()
        with self._state:
            reason = self._fault
        if reason is not None:
            # Never hold the state lock or a buffered stream lock while writing.
            with suppress(OSError):
                os.write(2, f"Collection journal storage fault: {reason}\n".encode())

    def _expire(self, now: float) -> None:
        first = next(iter(self._pending.values()), None)
        if first is not None and now >= first.deadline:
            self._fail("journal_ack_timeout")

    def submit(
        self,
        run_id: str,
        record: dict[str, object],
        *,
        event: bool = False,
        initial: bool = False,
    ) -> Ticket | None:
        with self._state:
            now = time.monotonic()
            self._expire(now)
            if self._fault is not None or self._closing.is_set():
                if event:
                    self._lost += 1
                return None
            self._operation += 1
            ticket = Ticket(
                self.instance_id, run_id, self._operation, now + ACK_SECONDS
            )
            try:
                self._queue.put_nowait(Job(ticket, record, event, initial))
            except queue.Full:
                self._fail("journal_queue_full")
                if event:
                    self._lost += 1
                return None
            self._pending[ticket.operation] = ticket
            return ticket

    def status(self) -> tuple[str | None, int]:
        with self._state:
            self._expire(time.monotonic())
            return self._fault, self._lost

    def acknowledged(self, ticket: Ticket) -> bool:
        with self._state:
            self._expire(time.monotonic())
            return (
                self._fault is None
                and ticket.instance_id == self.instance_id
                and ticket.committed
            )

    def close(self) -> bool:
        with self._state:
            now = time.monotonic()
            self._expire(now)
            self._closing.set()
            last = next(reversed(self._pending.values()), None)
            timeout = (
                max(0.0, last.deadline - now)
                if self._fault is None and last is not None
                else 0.15
            )
        self._thread.join(timeout=timeout)
        with self._state:
            self._expire(time.monotonic())
            if self._thread.is_alive() or self._pending:
                self._fail("journal_shutdown_incomplete")
            healthy = self._fault is None
            self._fault_notice.set()
        self._reporter.join(timeout=0.1)
        return healthy

    def _write(self) -> None:
        while True:
            try:
                if self._closing.is_set():
                    job = self._queue.get_nowait()
                else:
                    job = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._closing.is_set():
                    return
                continue
            with self._state:
                self._expire(time.monotonic())
                healthy = self._fault is None
            if not healthy:
                if job.event:
                    with self._state:
                        self._lost += 1
                continue
            try:
                self._persist(job)
            except (OSError, ValueError):
                with self._state:
                    self._fail("journal_write_failed")
                    if job.event:
                        self._lost += 1
                continue
            with self._state:
                self._expire(time.monotonic())
                self._pending.pop(job.ticket.operation)
                if self._fault is None:
                    job.ticket.committed = True

    def _persist(self, job: Job) -> None:
        run = self.root / job.ticket.run_id
        if job.initial:
            if self.root.is_symlink():
                raise OSError("journal root must not be a symlink")
            self.root.mkdir(mode=0o700, exist_ok=True)
            sync_directory(self.root.parent)
            run.mkdir(mode=0o700)
            sync_directory(self.root)
            with (run / "events.jsonl").open("xb") as stream:
                os.fsync(stream.fileno())
        if job.event:
            with (run / "events.jsonl").open("ab") as stream:
                stream.write(
                    json.dumps(
                        job.record, ensure_ascii=True, separators=(",", ":")
                    ).encode()
                    + b"\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
        else:
            write_snapshot(run / "state.json", job.record)
