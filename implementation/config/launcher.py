"""Foreground owner of the local web and collection processes."""

import os
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from collection.selection import InvalidSelection, parse
from config.control import (
    ADMISSION_FD,
    CONTROL_PATH,
    IO_TIMEOUT,
    ControlError,
    encode,
    receive,
)
from config.journal import JOURNAL_ROOT, JournalWriter, Ticket
from config.run_events import (
    INSTANCE_ID_ENV,
    RUN_ID_ENV,
    SCHEMA_VERSION,
    RunnerReader,
    timestamp,
)
from config.startup import web_command
from config.volume_owner import claim_volume

TICK = 0.05
START_TIMEOUT = 10.0
STOP_TIMEOUT = 40.0
REAP_TIMEOUT = 5.0


# The bootstrap cannot reach the web or worker executable before its pidfd
# exists. Closing the only writer cancels the gated child without signalling it.
EXEC_AFTER_PIDFD = """
import os
import sys

gate = int(sys.argv[1])
permission = os.read(gate, 1)
if permission != b"1":
    raise SystemExit(125)
if sys.argv[2]:
    os.environ[sys.argv[2]] = str(gate)
else:
    os.close(gate)
os.execvp(sys.argv[3], sys.argv[3:])
"""


def open_pidfd(pid: int) -> int:
    open_fd = cast(Callable[[int], int] | None, getattr(os, "pidfd_open", None))
    if open_fd is None:
        raise OSError("Linux pidfd_open is unavailable")
    return open_fd(pid)


def send_pidfd(fd: int, signum: int) -> None:
    send = cast(
        Callable[[int, int], None] | None, getattr(signal, "pidfd_send_signal", None)
    )
    if send is None:
        raise OSError("Linux pidfd_send_signal is unavailable")
    send(fd, signum)


@dataclass
class Child:
    process: subprocess.Popen[bytes]
    pidfd: int | None
    admission: int | None = None

    @classmethod
    def spawn(
        cls,
        command: list[str],
        *,
        permitted: Callable[[], bool],
        events: bool = False,
        env: dict[str, str] | None = None,
    ) -> "Child":
        if not permitted():
            raise ControlError("launcher is shutting down")
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb"), os.fdopen(writer, "wb"):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    EXEC_AFTER_PIDFD,
                    str(reader),
                    ADMISSION_FD if events else "",
                    *command,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if events else None,
                pass_fds=(reader,),
                env=env,
            )
            pidfd = None
            admission = None
            try:
                pidfd = open_pidfd(process.pid)
                if permitted():
                    if events:
                        admission = os.dup(writer)
                    os.write(writer, b"1")
                else:
                    os.close(pidfd)
                    pidfd = None
            except OSError:
                if pidfd is not None:
                    os.close(pidfd)
                    pidfd = None
                if admission is not None:
                    os.close(admission)
                    admission = None
        if process.stdout is not None:
            os.set_blocking(process.stdout.fileno(), False)
        return cls(process, pidfd, admission)

    def decide_admission(self, *, permit: bool) -> None:
        fd = self.admission
        if fd is None:
            return
        self.admission = None
        try:
            if permit:
                os.write(fd, b"1")
        finally:
            os.close(fd)

    def send(self, sig: int) -> bool:
        if self.pidfd is None:
            return False
        try:
            send_pidfd(self.pidfd, sig)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        return True

    def close(self, *, reader_owns_stream: bool = False) -> None:
        self.decide_admission(permit=False)
        if self.process.stdout is not None and not reader_owns_stream:
            self.process.stdout.close()
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None


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
        if run is not None and run.final is not None:
            if self.journal.acknowledged(run.final):
                self.state = "finished"
                self.reason = run.result or ""
                self.current = run = None
        fault, _ = self.journal.status()
        if fault is not None and self.state != "unknown":
            if self.state != "finished" or fault not in (
                "journal_seal_failed",
                "journal_seal_timeout",
            ):
                self.fault(fault)
        if run is None:
            return
        child = run.child
        if child is None:
            if fault is not None:
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
            return
        reader = run.reader
        assert reader is not None
        code = child.process.poll()
        now = time.monotonic()
        if code is not None:
            reader.reaped()
            if run.reap_deadline is None:
                run.reap_deadline = now + REAP_TIMEOUT
        events, progress, invalid, drained = reader.collect()
        if invalid and self.state != "unknown":
            self.fault("invalid_runner_event")
        for event, reason in events:
            self.event(run, event, reason)
        if progress > run.progress:
            run.progress = progress
            if self.state == "running":
                self.snapshot(run, self.state)
        if code is not None and (drained or now >= (run.reap_deadline or now)):
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
            return
        if code is not None:
            return
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


class ControlHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        connection = cast(socket.socket, self.request)
        server = cast(ControlServer, self.server)
        try:
            command = receive(connection, time.monotonic() + IO_TIMEOUT)
            response = server.supervisor.dispatch(command)
        except (ControlError, OSError) as error:
            response = {"ok": False, "reason": str(error)}
        try:
            connection.settimeout(IO_TIMEOUT)
            connection.sendall(encode(response))
        except (ControlError, OSError):
            return


class ControlServer(socketserver.UnixStreamServer):
    timeout = TICK

    def __init__(self, path: Path, supervisor: Supervisor) -> None:
        self.supervisor = supervisor
        # Never replace a predecessor's endpoint. The Docker tmpfs owns its lifetime.
        if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
            raise ControlError("control directory must be private (0700)")
        old_mask = os.umask(0o177)
        try:
            super().__init__(str(path), ControlHandler)
        finally:
            os.umask(old_mask)
        self.path = path

    def server_close(self) -> None:
        super().server_close()
        if hasattr(self, "path"):
            self.path.unlink(missing_ok=True)


def serve(
    path: Path,
    web_command: list[str],
    runner_command: list[str],
    *,
    journal_root: Path = JOURNAL_ROOT,
    recover: bool = False,
) -> int:
    # Probe both calls before spawning anything. No numerical-PID fallback.
    fd = open_pidfd(os.getpid())
    try:
        send_pidfd(fd, 0)
    finally:
        os.close(fd)

    supervisor = Supervisor(runner_command, journal_root, recover=recover)

    def shutdown(_signum: int, _frame: object) -> None:
        supervisor.shutting_down = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)

    def can_spawn() -> bool:
        return not supervisor.shutting_down

    try:
        with ControlServer(path, supervisor) as server:
            while supervisor.recovering and can_spawn():
                supervisor.tick()
                if supervisor.journal.status()[0] is not None:
                    return 1
                if supervisor.recovering:
                    server.handle_request()
            if supervisor.recovering:
                return 1
            if not can_spawn():
                return 0
            web = Child.spawn(web_command, permitted=can_spawn)
            if web.pidfd is None:
                web.process.wait(timeout=REAP_TIMEOUT)
                web.close()
                if supervisor.shutting_down:
                    return 0
                raise ControlError("cannot supervise web without its pidfd")
            result = 0
            try:
                while not supervisor.shutting_down and web.process.poll() is None:
                    server.handle_request()
                    supervisor.tick()
                if not supervisor.shutting_down:
                    result = 1
            finally:
                supervisor.shutting_down = True
                supervisor.stop()
                web.send(signal.SIGTERM)
                deadline = time.monotonic() + STOP_TIMEOUT
                while time.monotonic() < deadline + REAP_TIMEOUT:
                    supervisor.tick()
                    if web.process.poll() is not None and supervisor.current is None:
                        break
                    if time.monotonic() >= deadline:
                        web.send(signal.SIGKILL)
                    server.handle_request()
                if web.process.poll() is None or supervisor.current is not None:
                    result = 1
                web.close()
    finally:
        storage_closed = supervisor.close()
    return result if storage_closed else 1


def main() -> int:
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    os.environ.setdefault("PGCONNECT_TIMEOUT", "5")
    try:
        # Held until the OS exits this launcher, even if a daemon is stuck in I/O.
        claim_volume(JOURNAL_ROOT.parent)
        preflight = subprocess.run(
            [sys.executable, "-m", "config.startup", "--check-only"],
            check=False,
        )
        if preflight.returncode:
            return 1
        return serve(
            CONTROL_PATH,
            web_command(),
            [sys.executable, "-m", "collection.runner"],
            recover=True,
        )
    except (ControlError, OSError):
        print("Launcher could not initialize local process control.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
