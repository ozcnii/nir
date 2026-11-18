import argparse
import hashlib
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from host_runner.docker import DockerError
from host_runner.leases import HEARTBEAT_INTERVAL, LeaseError, LeaseStore
from host_runner.process import birth

TASK_STOP_GRACE = 5.0
TASK_EXIT_FLAGS = os.WEXITED | os.WNOHANG | os.WNOWAIT

# The bootstrap uses the task's eventual PID and group. EOF is cancellation:
# a dead runner must never authorize execution before recording the group.
EXEC_AFTER_REGISTRATION = """
import os
import sys

gate = int(sys.argv[1])
permission = os.read(gate, 1)
os.close(gate)
if permission != b"1":
    raise SystemExit(125)
try:
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
except OSError as error:
    print(f"host_runner: {error}", file=sys.stderr)
    raise SystemExit(1)
"""


def task_signal(pid: int, signum: int) -> None:
    # The unreaped Popen leader reserves its PID while we address its task group.
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        return
    except PermissionError:
        # Darwin can return EPERM when only the exited leader remains.
        # Any surviving group members still prevent release after we reap.
        if os.waitid(os.P_PID, pid, TASK_EXIT_FLAGS) is None:
            raise


def terminal_group() -> int | None:
    group = os.getpgrp()
    if os.isatty(0):
        with suppress(OSError):
            if os.tcgetpgrp(0) == group:
                return group
    return None


def give_terminal(group: int) -> None:
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    try:
        os.tcsetpgrp(0, group)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def run_task(
    store: LeaseStore,
    token: str,
    guard: int,
    command: list[str],
    env: dict[str, str],
    pending_signal: Callable[[], int | None],
) -> tuple[int, bool]:
    foreground = terminal_group()
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb"), os.fdopen(writer, "wb"):
        with subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                EXEC_AFTER_REGISTRATION,
                str(reader),
                *command,
            ],
            cwd=store.root,
            env=env,
            pass_fds=(guard, reader),
            process_group=0,
        ) as task:
            handed_off = False
            reaped = False
            try:
                store.register_group(token, task.pid)
                if foreground is not None:
                    give_terminal(task.pid)
                    handed_off = True
                if pending_signal() is None:
                    os.write(writer, b"1")
                next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
                while True:
                    # Observe exits without reaping. The leader's PID must stay
                    # reserved until we finish sending signals to its group.
                    try:
                        result = os.waitid(os.P_PID, task.pid, TASK_EXIT_FLAGS)
                    except ChildProcessError:
                        # Without an owned leader, do not signal the numeric group.
                        reaped = True
                        raise
                    signum = pending_signal()
                    if signum is None and result is not None:
                        if result.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                            if result.si_status in (signal.SIGINT, signal.SIGTERM):
                                signum = result.si_status
                        elif result.si_code == os.CLD_EXITED and result.si_status in (
                            128 + signal.SIGINT,
                            128 + signal.SIGTERM,
                        ):
                            # A foreground handler can translate its signal into
                            # an exit code without the host receiving it.
                            signum = result.si_status - 128
                    if signum is not None:
                        task_signal(task.pid, signum)
                        # The direct child may already be a zombie while a
                        # same-group descendant is still handling this signal.
                        time.sleep(TASK_STOP_GRACE)
                        task_signal(task.pid, signal.SIGKILL)
                        task.wait()
                        reaped = True
                        return 128 + signum, True
                    if result is not None:
                        status = task.wait()
                        reaped = True
                        return (128 - status if status < 0 else status), False
                    if time.monotonic() >= next_heartbeat:
                        store.heartbeat(token)
                        next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
                    time.sleep(0.1)
            finally:
                try:
                    if not reaped:
                        task_signal(task.pid, signal.SIGKILL)
                finally:
                    if handed_off and foreground is not None:
                        give_terminal(foreground)


def run(root: Path, command: list[str], db_only: bool) -> int:
    owner_birth = birth(os.getpid())
    if owner_birth is None:
        raise LeaseError("Cannot verify the runner process identity")
    project = f"nir-{hashlib.sha256(os.fsencode(root)).hexdigest()[:16]}"
    store = LeaseStore(root, project)
    token = uuid.uuid4().hex
    requested: int | None = None
    interrupted = False

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal requested
        requested = signum

    def pending_signal() -> int | None:
        return requested

    previous = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    previous[signal.SIGCHLD] = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        with store.hold_task(token) as guard:
            url, generation = store.acquire(token, owner_birth, db_only)
            if (signum := pending_signal()) is not None:
                return 128 + signum
            env = {
                **os.environ,
                "COMPOSE_PROJECT_NAME": project,
                "NIR_RUNNER_GENERATION": generation or "unmanaged",
            }
            env.pop("NIR_APP_URL", None)
            if url is not None:
                env["NIR_APP_URL"] = url
                print(url, flush=True)
            status, interrupted = run_task(
                store, token, guard.fileno(), command, env, pending_signal
            )
            return status
    finally:
        try:
            store.release(
                token, wait_for_task=interrupted or pending_signal() is not None
            )
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a task with a local Docker lease")
    parser.add_argument(
        "--db-only", action="store_true", help="require only PostgreSQL"
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command: list[str] = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("pass a foreground command after --")
    try:
        return run(Path(__file__).resolve().parents[1], command, args.db_only)
    except (DockerError, LeaseError, OSError, ValueError, KeyError) as error:
        print(f"host_runner: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
