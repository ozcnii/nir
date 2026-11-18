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
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from collection.selection import InvalidSelection, parse
from config.control import (
    CONTROL_PATH,
    IO_TIMEOUT,
    MAX_MESSAGE,
    ControlError,
    decode,
    encode,
    receive,
)
from config.startup import web_command

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
os.close(gate)
if permission != b"1":
    raise SystemExit(125)
os.execvp(sys.argv[2], sys.argv[2:])
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

    @classmethod
    def spawn(
        cls,
        command: list[str],
        *,
        permitted: Callable[[], bool],
        events: bool = False,
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
                    *command,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if events else None,
                pass_fds=(reader,),
            )
            pidfd = None
            try:
                pidfd = open_pidfd(process.pid)
                if permitted():
                    os.write(writer, b"1")
                else:
                    os.close(pidfd)
                    pidfd = None
            except OSError:
                if pidfd is not None:
                    os.close(pidfd)
                    pidfd = None
        if process.stdout is not None:
            os.set_blocking(process.stdout.fileno(), False)
        return cls(process, pidfd)

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

    def close(self) -> None:
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None


@dataclass
class CurrentRun:
    child: Child
    deadline: float
    buffer: bytearray = field(default_factory=bytearray)
    result: str | None = None
    armed: bool = False
    events_valid: bool = True
    stop_at: float | None = None
    kill_at: float | None = None


class Supervisor:
    def __init__(self, runner: list[str]) -> None:
        self.runner = runner
        self.state = "idle"
        self.reason = ""
        self.run_id: str | None = None
        self.current: CurrentRun | None = None
        self.shutting_down = False

    def status(self, *, ok: bool = True, error: str = "") -> dict[str, object]:
        return {
            "ok": ok,
            "state": self.state,
            "run_id": self.run_id,
            "cleanup_confirmed": not self.shutting_down
            and self.state in ("idle", "finished"),
            "reason": error or self.reason,
        }

    def dispatch(self, message: dict[str, object]) -> dict[str, object]:
        self.tick()
        action = message.get("action")
        if action == "status" and set(message) == {"action"}:
            return self.status()
        if action == "stop" and set(message) == {"action", "run_id"}:
            if self.run_id is None or message["run_id"] != self.run_id:
                return self.status(ok=False, error="unknown_run")
            self.stop()
            return self.status()
        if action != "start" or set(message) != {"action", "selection"}:
            raise ControlError("unsupported control request")
        if self.shutting_down or self.state not in ("idle", "finished"):
            return self.status(ok=False, error="collection_unavailable")
        try:
            parse(message["selection"])
        except InvalidSelection as error:
            return self.status(ok=False, error=str(error))
        self.run_id = uuid.uuid4().hex
        self.state = "starting"
        self.reason = ""
        try:
            snapshot = (
                encode(cast(dict[str, object], message["selection"])).decode().strip()
            )
            child = Child.spawn(
                [*self.runner, snapshot],
                permitted=lambda: not self.shutting_down,
                events=True,
            )
        except (ControlError, OSError):
            self.state = "unknown"
            return self.status(ok=False, error="spawn_failed")
        self.current = CurrentRun(child, time.monotonic() + START_TIMEOUT)
        if child.pidfd is None:
            self.fault("pidfd_unavailable")
            return self.status(ok=False)
        return self.status()

    def stop(self) -> None:
        run = self.current
        if run is None or run.stop_at is not None:
            return
        run.stop_at = time.monotonic()
        if self.state != "unknown":
            self.state = "stopping"
        if run.armed:
            self.send_term(run)

    def send_term(self, run: CurrentRun) -> None:
        if not run.child.send(signal.SIGTERM):
            self.state = "unknown"
            self.reason = "stop_not_confirmed"

    def fault(self, reason: str) -> None:
        self.state = "unknown"
        self.reason = reason
        self.stop()

    def reject_events(self, run: CurrentRun, reason: str) -> None:
        run.events_valid = False
        run.buffer.clear()
        self.fault(reason)

    def read_events(self, run: CurrentRun) -> None:
        output = run.child.process.stdout
        if output is None:
            self.reject_events(run, "missing_runner_events")
            return
        try:
            chunk = os.read(output.fileno(), MAX_MESSAGE - len(run.buffer))
        except BlockingIOError:
            return
        except OSError:
            self.reject_events(run, "runner_pipe_error")
            return
        if not run.events_valid:
            # Discard after a protocol violation, so output cannot prevent reaping.
            return
        run.buffer.extend(chunk)
        while (end := run.buffer.find(b"\n")) >= 0:
            line = bytes(run.buffer[:end])
            del run.buffer[: end + 1]
            try:
                self.event(run, decode(line))
            except ControlError:
                self.reject_events(run, "invalid_runner_event")
                return
        if len(run.buffer) >= MAX_MESSAGE:
            self.reject_events(run, "invalid_runner_event")

    def event(self, run: CurrentRun, message: dict[str, object]) -> None:
        event = message.get("event")
        reason = message.get("reason")
        if (
            set(message) != {"event", "reason"}
            or not isinstance(reason, str)
            or len(reason) > 64
            or run.result is not None
        ):
            raise ControlError("invalid runner event")
        if event == "armed" and not run.armed:
            run.armed = True
            if run.stop_at is not None:
                self.send_term(run)
        elif event == "ready" and run.armed and self.state == "starting":
            self.state = "running"
        elif event == "ready" and run.armed and self.state == "stopping":
            # A ready event may already be queued when stop is requested.
            return
        elif event == "finished" and run.armed:
            run.result = reason
        elif event == "fault":
            self.fault(reason)
        else:
            raise ControlError("unexpected runner event")

    def tick(self) -> None:
        run = self.current
        if run is None:
            return
        self.read_events(run)
        code = run.child.process.poll()
        if code is not None:
            self.read_events(run)
            if (
                code == 0
                and run.result is not None
                and not run.buffer
                and self.state != "unknown"
            ):
                self.state = "finished"
                self.reason = run.result
            elif self.state != "unknown":
                self.state = "unknown"
                self.reason = "cleanup_not_confirmed"
            run.child.close()
            self.current = None
            return
        now = time.monotonic()
        if self.state == "starting" and now >= run.deadline:
            self.fault("initialization_timeout")
        if (
            run.stop_at is not None
            and run.kill_at is None
            and now >= run.stop_at + STOP_TIMEOUT
        ):
            run.kill_at = now
            self.state = "unknown"
            self.reason = "forced_stop"
            if not run.child.send(signal.SIGKILL):
                self.reason = "stop_not_confirmed"
        if run.kill_at is not None and now >= run.kill_at + REAP_TIMEOUT:
            self.reason = "cleanup_not_confirmed"


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


def serve(path: Path, web_command: list[str], runner_command: list[str]) -> int:
    # Probe both calls before spawning anything. No numerical-PID fallback.
    fd = open_pidfd(os.getpid())
    try:
        send_pidfd(fd, 0)
    finally:
        os.close(fd)

    supervisor = Supervisor(runner_command)

    def shutdown(_signum: int, _frame: object) -> None:
        supervisor.shutting_down = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)

    def can_spawn() -> bool:
        return not supervisor.shutting_down

    with ControlServer(path, supervisor) as server:
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
                supervisor.tick()
                server.handle_request()
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
    return result


def main() -> int:
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    os.environ.setdefault("PGCONNECT_TIMEOUT", "5")
    preflight = subprocess.run(
        [sys.executable, "-m", "config.startup", "--check-only"],
        check=False,
    )
    if preflight.returncode:
        return 1
    try:
        return serve(
            CONTROL_PATH, web_command(), [sys.executable, "-m", "collection.runner"]
        )
    except (ControlError, OSError) as error:
        print(f"Launcher: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
