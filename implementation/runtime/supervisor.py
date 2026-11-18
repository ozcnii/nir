"""Manage collection admission, progress and shutdown."""

import os
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from collection.selection import InvalidSelection, parse
from runtime.child import Child
from runtime.control import ControlError, encode
from runtime.journal import JOURNAL_ROOT, JournalWriter, Ticket
from runtime.run_events import (
    INSTANCE_ID_ENV,
    RUN_ID_ENV,
    SCHEMA_VERSION,
    RunnerReader,
    timestamp,
)

START_TIMEOUT = 10.0
STOP_TIMEOUT = 40.0
REAP_TIMEOUT = 5.0


@dataclass
class CurrentRun:
    id: str
    selection: dict[str, object]
    started_at: str
    initial: Ticket | None = None
    final: Ticket | None = None
    child: Child | None = None
    reader: RunnerReader | None = None
    start_deadline: float = 0.0
    reap_deadline: float | None = None
    result: str | None = None
    progress: int = 0
    armed: bool = False
    admitted: bool = False
    stop_at: float | None = None
    kill_at: float | None = None


class Supervisor:
    def __init__(
        self,
        runner: list[str],
        journal_root: Path = JOURNAL_ROOT,
        *,
        recover: bool = False,
    ) -> None:
        self.runner = runner
        self.instance_id = uuid.uuid4().hex
        self.journal = JournalWriter(journal_root, self.instance_id, recover=recover)
        self.recovering = recover
        self.state = "unknown" if recover else "idle"
        self.reason = ""
        self.run_id: str | None = None
        self.current: CurrentRun | None = None
        self.shutting_down = False

    def status(self, *, ok: bool = True, error: str = "") -> dict[str, object]:
        storage_fault, lost = self.journal.status()
        return {
            "ok": ok,
            "state": self.state,
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "cleanup_confirmed": not self.shutting_down
            and storage_fault is None
            and self.state in ("idle", "finished"),
            "reason": error or storage_fault or self.reason,
            "storage_fault": storage_fault is not None,
            "lost_events": lost,
        }

    def snapshot(
        self,
        run: CurrentRun,
        state: str,
        *,
        reason: str = "",
        outcome: str | None = None,
        initial: bool = False,
    ) -> Ticket | None:
        now = timestamp()
        return self.journal.submit(
            run.id,
            {
                "schema_version": SCHEMA_VERSION,
                "instance_id": self.instance_id,
                "run_id": run.id,
                "status": state,
                "outcome": outcome,
                "reason": reason,
                "started_at": run.started_at,
                "updated_at": now,
                "finished_at": now if state == "finished" else None,
                "selection": run.selection,
                "progress": {"committed_writes": run.progress},
                "cleanup_confirmed": state == "finished",
            },
            initial=initial,
        )

    def dispatch(self, message: dict[str, object]) -> dict[str, object]:
        action = message.get("action")
        if action == "stop" and set(message) == {"action", "run_id"}:
            if self.run_id is None or message["run_id"] != self.run_id:
                return self.status(ok=False, error="unknown_run")
            # A received stop takes precedence over buffered arming or initial ACK.
            self.stop()
            return self.status()
        self.tick()
        if action == "status" and set(message) == {"action"}:
            return self.status()
        if action != "start" or set(message) != {"action", "selection"}:
            raise ControlError("unsupported control request")
        if (
            self.shutting_down
            or self.state not in ("idle", "finished")
            or self.journal.status()[0] is not None
        ):
            return self.status(ok=False, error="collection_unavailable")
        try:
            selection = parse(message["selection"])
        except InvalidSelection as error:
            return self.status(ok=False, error=str(error))
        self.run_id = uuid.uuid4().hex
        self.state = "starting"
        self.reason = ""
        run = self.current = CurrentRun(self.run_id, selection.record(), timestamp())
        run.initial = self.snapshot(run, "starting", initial=True)
        return self.status(ok=run.initial is not None)

    def spawn(self, run: CurrentRun) -> None:
        try:
            child = Child.spawn(
                [*self.runner, encode(run.selection).decode().strip()],
                permitted=lambda: not self.shutting_down,
                events=True,
                env={
                    **os.environ,
                    RUN_ID_ENV: run.id,
                    INSTANCE_ID_ENV: self.instance_id,
                },
            )
        except (ControlError, OSError):
            self.unknown(run, "spawn_failed")
            return
        run.child = child
        run.start_deadline = time.monotonic() + START_TIMEOUT
        assert child.process.stdout is not None
        run.reader = RunnerReader(child.process.stdout, self.journal, run.id)
        if child.pidfd is None:
            self.fault("pidfd_unavailable")

    def stop(self) -> None:
        run = self.current
        if run is None or run.stop_at is not None or run.final is not None:
            return
        run.stop_at = time.monotonic()
        child = run.child
        if child is not None:
            if run.admitted:
                if not child.send(signal.SIGTERM):
                    self.state = "unknown"
                    self.reason = "stop_not_confirmed"
            else:
                child.decide_admission(permit=False)
        if self.state != "unknown":
            self.state = "stopping"
            self.snapshot(run, self.state)

    def fault(self, reason: str) -> None:
        self.state = "unknown"
        self.reason = reason
        self.stop()

    def unknown(self, run: CurrentRun, reason: str) -> None:
        self.state = "unknown"
        self.reason = reason
        self.snapshot(run, self.state, reason=reason, outcome="error")
        self.current = None

    def finalize(self, run: CurrentRun, reason: str) -> None:
        self.state = "stopping"
        outcome = (
            reason
            if reason in ("completed", "completed_with_errors", "stopped")
            else "error"
        )
        run.result = reason
        run.final = self.snapshot(run, "finished", reason=reason, outcome=outcome)

    def event(self, run: CurrentRun, event: str, reason: str) -> None:
        child = run.child
        assert child is not None
        if event == "armed" and not run.armed:
            run.armed = True
            if (
                self.shutting_down
                or run.stop_at is not None
                or self.journal.status()[0]
            ):
                self.stop()
                return
            try:
                child.decide_admission(permit=True)
                run.admitted = True
            except OSError:
                self.fault("runner_admission_failed")
        elif event == "ready" and run.admitted and self.state == "starting":
            self.state = "running"
            self.snapshot(run, self.state)
        elif (
            event == "ready" and run.admitted and self.state in ("stopping", "unknown")
        ):
            return
        elif event == "finished" and run.armed:
            run.result = reason
        elif event == "fault":
            self.fault(reason)
        else:
            self.fault("invalid_runner_event")

    def tick(self) -> None:
        if self.recovering:
            if self.journal.recovery_complete():
                self.recovering = False
                self.state = "idle"
            return
        run = self.current
        if run and run.final and self.journal.acknowledged(run.final):
            self.state = "finished"
            self.reason = run.result or ""
            self.current = run = None
        fault, _ = self.journal.status()
        if (
            fault is not None
            and self.state != "unknown"
            and not (
                self.state == "finished"
                and fault in ("journal_seal_failed", "journal_seal_timeout")
            )
        ):
            self.fault(fault)
        if run is None:
            return
        if run.child is not None:
            self._tick_child(run, run.child)
        elif fault is not None:
            self.current = None
        elif (
            run.final is None
            and run.initial is not None
            and self.journal.acknowledged(run.initial)
        ):
            if self.shutting_down or run.stop_at is not None:
                self.finalize(run, "stopped")
            else:
                self.spawn(run)

    def _tick_child(self, run: CurrentRun, child: Child) -> None:
        reader = run.reader
        assert reader is not None
        code = child.process.poll()
        now = time.monotonic()
        if code is not None:
            reader.reaped()
            if run.reap_deadline is None:
                run.reap_deadline = now + REAP_TIMEOUT
        events, progress, invalid, drained = reader.collect()
        self._apply_runner_updates(run, events, progress, invalid)
        if code is None:
            self._check_child_deadlines(run, child, now, drained)
            return
        if not drained and now < (run.reap_deadline or now):
            return
        reader.close()
        child.close(reader_owns_stream=True)
        run.child = None
        if (
            code == 0
            and drained
            and not invalid
            and run.result is not None
            and self.state != "unknown"
        ):
            self.finalize(run, run.result)
        else:
            self.unknown(
                run,
                self.reason if self.state == "unknown" else "cleanup_not_confirmed",
            )

    def _apply_runner_updates(
        self,
        run: CurrentRun,
        events: list[tuple[str, str]],
        progress: int,
        invalid: bool,
    ) -> None:
        if invalid and self.state != "unknown":
            self.fault("invalid_runner_event")
        for event, reason in events:
            self.event(run, event, reason)
        if progress > run.progress:
            run.progress = progress
            if self.state == "running":
                self.snapshot(run, self.state)

    def _check_child_deadlines(
        self, run: CurrentRun, child: Child, now: float, drained: bool
    ) -> None:
        if drained and run.result is None and self.state != "unknown":
            self.fault("runner_pipe_closed")
        if self.state == "starting" and now >= run.start_deadline:
            self.fault("initialization_timeout")
        if (
            run.stop_at is not None
            and run.kill_at is None
            and now >= run.stop_at + STOP_TIMEOUT
        ):
            run.kill_at = now
            self.state = "unknown"
            self.reason = (
                "forced_stop" if child.send(signal.SIGKILL) else "stop_not_confirmed"
            )
        if run.kill_at is not None and now >= run.kill_at + REAP_TIMEOUT:
            self.reason = "cleanup_not_confirmed"

    def close(self) -> bool:
        run = self.current
        if run is not None and run.reader is not None:
            run.reader.close()
        if not self.journal.close():
            self.state = "unknown"
            return False
        return True
