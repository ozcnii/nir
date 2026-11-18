"""Spawn and signal a child using its process descriptor."""

import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from runtime.control import ADMISSION_FD, ControlError

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
