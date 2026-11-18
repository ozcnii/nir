import argparse
import hashlib
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

from host_runner.docker import DockerError
from host_runner.leases import HEARTBEAT_INTERVAL, LeaseError, LeaseStore
from host_runner.process import birth


def task_signal(pid: int, signum: int) -> None:
    # The unreaped Popen leader reserves its PID while we address its task group.
    with suppress(ProcessLookupError):
        os.killpg(pid, signum)


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


def run(root: Path, command: list[str], db_only: bool) -> int:
    owner_birth = birth(os.getpid())
    if owner_birth is None:
        raise LeaseError("Cannot verify the runner process identity")
    project = f"nir-{hashlib.sha256(os.fsencode(root)).hexdigest()[:16]}"
    store = LeaseStore(root, project)
    token = uuid.uuid4().hex
    requested: int | None = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal requested
        requested = signum

    def pending_signal() -> int | None:
        return requested

    previous = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
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
            foreground = terminal_group()
            with subprocess.Popen(
                command,
                cwd=root,
                env=env,
                pass_fds=(guard.fileno(),),
                process_group=0,
            ) as task:
                next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
                try:
                    store.register_group(token, task.pid)
                    if foreground is not None:
                        give_terminal(task.pid)
                        task_signal(task.pid, signal.SIGCONT)
                    while True:
                        if (signum := pending_signal()) is not None:
                            task_signal(task.pid, signum)
                            try:
                                task.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                task_signal(task.pid, signal.SIGKILL)
                            return 128 + signum
                        try:
                            status = task.wait(timeout=0.2)
                        except subprocess.TimeoutExpired:
                            if time.monotonic() >= next_heartbeat:
                                store.heartbeat(token)
                                next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
                        else:
                            signum = pending_signal()
                            if signum is not None:
                                return 128 + signum
                            return 128 - status if status < 0 else status
                finally:
                    try:
                        if task.poll() is None:
                            task_signal(task.pid, signal.SIGKILL)
                    finally:
                        if foreground is not None:
                            give_terminal(foreground)
    finally:
        try:
            store.release(token, wait_for_task=pending_signal() is not None)
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
