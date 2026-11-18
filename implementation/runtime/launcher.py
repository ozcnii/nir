"""Container entry point and local process control loop."""

import os
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from runtime.child import Child, open_pidfd, send_pidfd
from runtime.control import CONTROL_PATH, IO_TIMEOUT, ControlError, encode, receive
from runtime.journal import JOURNAL_ROOT
from runtime.startup import web_command
from runtime.supervisor import REAP_TIMEOUT, STOP_TIMEOUT, Supervisor
from runtime.volume_owner import claim_volume

TICK = 0.05


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
            [sys.executable, "-m", "runtime.startup", "--check-only"],
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
