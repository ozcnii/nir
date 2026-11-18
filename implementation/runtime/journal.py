"""Single owner of the launcher's on-volume journal writes."""

import json
import os
import queue
import threading
import time
import uuid
from collections.abc import Generator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from runtime.journal_history import (
    STATE_FILE,
    TERMINAL_SEAL,
    directory,
    open_regular,
    terminal_digest,
)
from runtime.journal_maintenance import append_event, recover_runs, retention_steps

JOURNAL_ROOT = Path("/var/lib/app/runs")
QUEUE_LIMIT = 256
ACK_SECONDS = 5.0


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


def write_file(run: int, name: str, data: bytes) -> None:
    temporary = f".{name}-{uuid.uuid4().hex}"
    fd = open_regular(run, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=run, dst_dir_fd=run)
        os.fsync(run)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=run)


def write_snapshot(run: int, record: dict[str, object]) -> None:
    raw = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    write_file(run, STATE_FILE, raw)


class JournalWriter:
    def __init__(self, root: Path, instance_id: str, *, recover: bool = False) -> None:
        self.root = root
        self.instance_id = instance_id
        self._queue: queue.Queue[Job] = queue.Queue(maxsize=QUEUE_LIMIT)
        self._state = threading.Lock()
        self._pending: dict[int, Ticket] = {}
        self._operation = 0
        self._maintenance: Generator[None] | None = None
        self._fault: str | None = None
        self._lost = 0
        self._recovery_deadline = time.monotonic() + ACK_SECONDS if recover else None
        self._seal_deadline: float | None = None
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
        if self._recovery_deadline is not None and now >= self._recovery_deadline:
            self._fail("journal_recovery_timeout")
        if self._seal_deadline is not None and now >= self._seal_deadline:
            self._fail("journal_seal_timeout")
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

    def recovery_complete(self) -> bool:
        with self._state:
            self._expire(time.monotonic())
            return self._recovery_deadline is None and self._fault is None

    def status(self) -> tuple[str | None, int]:
        with self._state:
            self._expire(time.monotonic())
            return self._fault, self._lost

    def acknowledged(self, ticket: Ticket) -> bool:
        with self._state:
            self._expire(time.monotonic())
            return ticket.instance_id == self.instance_id and ticket.committed

    def close(self) -> bool:
        with self._state:
            now = time.monotonic()
            self._expire(now)
            self._closing.set()
            last = next(reversed(self._pending.values()), None)
            deadline = last.deadline if last is not None else self._recovery_deadline
            if deadline is None:
                deadline = self._seal_deadline
            timeout = (
                max(0.0, deadline - now)
                if self._fault is None and deadline is not None
                else 0.15
            )
        self._thread.join(timeout=timeout)
        with self._state:
            self._expire(time.monotonic())
            if (
                self._thread.is_alive()
                or self._pending
                or self._recovery_deadline is not None
            ):
                self._fail("journal_shutdown_incomplete")
            healthy = self._fault is None
            self._fault_notice.set()
        self._reporter.join(timeout=0.1)
        return healthy

    def _write(self) -> None:
        if self._recovery_deadline is not None:
            try:
                recover_runs(self.root, self._write_recovered_state)
                self._maintenance = retention_steps(self.root)
            except OSError:
                with self._state:
                    self._fail("journal_recovery_failed")
            finally:
                with self._state:
                    self._expire(time.monotonic())
                    self._recovery_deadline = None
        while True:
            try:
                if self._closing.is_set() or self._maintenance is not None:
                    job = self._queue.get_nowait()
                else:
                    job = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._closing.is_set():
                    self._cancel_maintenance()
                    return
                self._maintain()
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
                sealed = self._persist(job)
            except (OSError, ValueError):
                with self._state:
                    self._fail("journal_write_failed")
                    if job.event:
                        self._lost += 1
                continue
            if sealed:
                self._cancel_maintenance()
                self._maintenance = retention_steps(self.root, job.ticket.run_id)

    def _cancel_maintenance(self) -> None:
        if self._maintenance is not None:
            self._maintenance.close()
            self._maintenance = None

    def _maintain(self) -> None:
        if self._maintenance is None:
            return
        with self._state:
            self._expire(time.monotonic())
            healthy = self._fault is None
        if not healthy:
            self._cancel_maintenance()
            return
        try:
            next(self._maintenance)
        except StopIteration:
            self._maintenance = None
        except OSError:
            self._cancel_maintenance()
            with self._state:
                self._fail("journal_retention_failed")

    def _write_recovered_state(self, run: int, record: dict[str, object]) -> None:
        write_snapshot(run, record)
        with self._state:
            self._expire(time.monotonic())
            timely_snapshot = self._fault is None
        if timely_snapshot:
            write_file(run, TERMINAL_SEAL, terminal_digest(record))

    def _commit(self, job: Job, *, terminal: bool) -> bool:
        with self._state:
            now = time.monotonic()
            self._expire(now)
            self._pending.pop(job.ticket.operation)
            job.ticket.committed = self._fault is None
            if terminal and job.ticket.committed:
                self._seal_deadline = now + ACK_SECONDS
            return job.ticket.committed

    def _seal(self, run: int, record: dict[str, object]) -> bool:
        failed = False
        try:
            write_file(run, TERMINAL_SEAL, terminal_digest(record))
        except (OSError, ValueError):
            failed = True
        with self._state:
            self._expire(time.monotonic())
            self._seal_deadline = None
            if failed:
                self._fail("journal_seal_failed")
            return self._fault is None

    def _persist(self, job: Job) -> bool:
        with directory(self.root.parent) as volume:
            if job.initial:
                with suppress(FileExistsError):
                    os.mkdir(self.root.name, 0o700, dir_fd=volume)
                os.fsync(volume)
            with directory(self.root.name, volume) as root:
                if job.initial:
                    os.mkdir(job.ticket.run_id, 0o700, dir_fd=root)
                with directory(job.ticket.run_id, root) as run:
                    if job.initial:
                        fd = open_regular(
                            run, "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        )
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                        os.fsync(root)
                    if job.event:
                        append_event(run, job.record)
                    else:
                        write_snapshot(run, job.record)
                    terminal = not job.event and job.record["status"] == "finished"
                    if self._commit(job, terminal=terminal) and terminal:
                        # The seal follows an irrevocable, timely snapshot ACK.
                        return self._seal(run, job.record)
        return False
