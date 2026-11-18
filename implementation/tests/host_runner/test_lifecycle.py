import fcntl
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

from host_runner.leases import State
from host_runner.process import birth, group_exists

REPOSITORY = Path(__file__).resolve().parents[2]
RUN = r"""
import os
import sys
import time
from pathlib import Path
from host_runner import leases
from host_runner import __main__ as host

def checked_birth(pid):
    if str(pid) == os.environ.get("FAKE_UNKNOWN_PID"):
        raise OSError("fixture process identity unavailable")
    return actual_birth(pid)

def checked_save(store, state):
    if state["phase"] == "ready" and os.environ.pop("FAKE_READY_WRITE", ""):
        raise OSError("fixture transient ready write failure")
    return actual_save(store, state)

def checked_group_registration(store, token, group):
    if os.environ.get("FAKE_GROUP_REGISTRATION"):
        Path("registration-paused").write_text(str(group))
        while not Path("register-now").exists():
            time.sleep(0.01)
    return actual_register_group(store, token, group)

actual_birth = leases.birth
actual_save = leases.LeaseStore.save
actual_register_group = leases.LeaseStore.register_group
leases.birth = checked_birth
leases.LeaseStore.save = checked_save
leases.LeaseStore.register_group = checked_group_registration
if os.environ.get("FAKE_GROUP_REGISTRATION"):
    host.EXEC_AFTER_REGISTRATION = (
        "from pathlib import Path\nPath('bootstrap-ready').touch()\n"
        + host.EXEC_AFTER_REGISTRATION
    )
host.TASK_STOP_GRACE = 1.5
raise SystemExit(host.run(Path.cwd(), sys.argv[2:], sys.argv[1] == 'db'))
"""
TASK = """
import json
import os
import sys
import time
from pathlib import Path

key = Path(sys.argv[1])
Path(f'{key}.ready').write_text(json.dumps({
    'url': os.environ.get('NIR_APP_URL'),
    'project': os.environ['COMPOSE_PROJECT_NAME'],
    'generation': os.environ['NIR_RUNNER_GENERATION'],
}))
while not Path(f'{key}.exit').exists():
    time.sleep(0.01)
"""


def until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for the fixture")
        time.sleep(0.01)


class HostRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.registry = self.root / "containers.json"
        self.registry.write_text("{}")
        self.events = self.root / "events"
        self.state = self.root / ".local/runtime/leases.json"
        docker = self.root / "docker"
        source = Path(__file__).with_name("docker_cli_fixture.py")
        docker.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(source))} "$@"\n'
        )
        docker.chmod(0o700)
        self.env = {
            **os.environ,
            "PATH": f"{self.root}:{os.environ['PATH']}",
            "FAKE_DOCKER": str(self.root),
            "PYTHONPATH": str(REPOSITORY),
        }
        self.env.pop("APP_PORT", None)

    def start(
        self, name: str, script: str = TASK, mode: str = "full"
    ) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [sys.executable, "-c", RUN, mode, sys.executable, "-c", script, name],
            cwd=self.root,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(self.reap, process)
        return process

    @staticmethod
    def reap(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)

    def ready(self, name: str) -> dict[str, str | int | None]:
        marker = self.root / f"{name}.ready"
        # The task creates a small JSON marker; wait for its write, not just open().
        until(lambda: marker.exists() and marker.stat().st_size > 0)
        return cast(dict[str, str | int | None], json.loads(marker.read_text()))

    def finish(self, process: subprocess.Popen[bytes], name: str) -> None:
        (self.root / f"{name}.exit").touch()
        self.assertEqual(process.wait(timeout=10), 0)

    def read_state(self) -> State:
        return cast(State, json.loads(self.state.read_text()))

    def update_state(self, state: State) -> None:
        self.state.write_text(json.dumps(state))

    def wait_for_task_guard(self, token: str) -> None:
        path = self.state.parent / f"task-{token}.lock"
        group = self.read_state()["leases"][token].get("task_group")

        def unlocked() -> bool:
            if group is not None and group_exists(group):
                return False
            with path.open("rb") as guard:
                try:
                    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                return True

        until(unlocked)

    def assert_released(self) -> None:
        self.assertFalse(self.state.exists())
        self.assertEqual(self.registry.read_text(), "{}")

    def test_two_leases_keep_live_owner_with_expired_heartbeat(self) -> None:
        # Arrange
        first = self.start("first")
        first_env = self.ready("first")
        original = self.read_state()
        owner = next(iter(original["leases"].values()))
        self.assertEqual(owner["birth"], birth(first.pid))
        owner["heartbeat"] = 0
        self.update_state(original)

        # Act
        second = self.start("second")
        # Assert: reuse the stack and keep it through the first release.
        self.assertEqual(self.ready("second")["generation"], first_env["generation"])
        self.assertEqual(len(self.read_state()["leases"]), 2)
        self.assertEqual(self.read_state()["containers"], original["containers"])
        self.finish(second, "second")
        self.assertNotIn("container stop", self.events.read_text())

        # Act: release the last lease.
        self.finish(first, "first")
        # Assert
        self.assert_released()
        stops = [
            line
            for line in self.events.read_text().splitlines()
            if "container stop" in line
        ]
        generation = first_env["generation"]
        self.assertEqual(
            stops,
            [f"container stop {generation}-app", f"container stop {generation}-db"],
        )

    def test_pid_reuse_requires_birth_match_even_with_a_live_pid(self) -> None:
        # Arrange
        old = self.start("old")
        data = self.ready("old")
        old.kill()
        old.wait(timeout=10)
        token = next(iter(self.read_state()["leases"]))
        (self.root / "old.exit").touch()
        self.wait_for_task_guard(token)
        state = self.read_state()
        owner = next(iter(state["leases"].values()))
        owner["pid"] = os.getpid()
        owner["heartbeat"] = 0
        self.update_state(state)

        # Act
        new = self.start("new")
        # Assert
        self.assertNotEqual(self.ready("new")["generation"], data["generation"])
        self.finish(new, "new")
        self.assert_released()

    def test_sigkill_does_not_stop_a_surviving_task_then_recovers(self) -> None:
        # Arrange
        old = self.start("old")
        data = self.ready("old")
        token = next(iter(self.read_state()["leases"]))
        self.addCleanup((self.root / "old.exit").touch)
        # Act: kill the lease owner, leaving the task active.
        old.kill()
        old.wait(timeout=10)

        other = self.start("other")
        # Assert: another lease must reuse the protected stack.
        self.assertEqual(self.ready("other")["generation"], data["generation"])
        self.finish(other, "other")
        self.assertNotIn("container stop", self.events.read_text())

        # Act: let the orphan task finish before recovery.
        (self.root / "old.exit").touch()
        self.wait_for_task_guard(token)
        new = self.start("new")
        # Assert
        self.assertNotEqual(self.ready("new")["generation"], data["generation"])
        self.finish(new, "new")
        self.assert_released()

    def test_acquire_waits_for_the_last_release_to_finish(self) -> None:
        # Arrange
        old = self.start("old")
        first = self.ready("old")
        (self.root / "pause-stop").touch()
        self.addCleanup((self.root / "resume-stop").touch)
        (self.root / "old.exit").touch()
        until(lambda: "container stop" in self.events.read_text())
        self.assertEqual(self.read_state()["phase"], "stopping")

        # Act
        new = self.start("new")
        # The contender process has reached the runner invocation, while Docker's stop is blocked.
        until(
            lambda: (
                new.poll() is not None
                or len(list(self.state.parent.glob("task-*.lock"))) == 1
            )
        )
        # Assert: the acquire is still waiting.
        self.assertIsNone(new.poll())
        self.assertFalse((self.root / "new.ready").exists())
        # Act: allow the last release to finish.
        (self.root / "resume-stop").touch()
        # Assert
        self.assertEqual(old.wait(timeout=10), 0)
        self.assertNotEqual(self.ready("new")["generation"], first["generation"])
        self.finish(new, "new")
        self.assert_released()

    def test_failed_task_and_handled_signals_release_the_stack(self) -> None:
        # Arrange: setUp provides an idle host and a fresh registry.
        # Act: run a task that exits with an error.
        failure = self.start("failure", "raise SystemExit(17)")
        # Assert
        self.assertEqual(failure.wait(timeout=10), 17)
        self.assert_released()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                # Arrange
                name = signum.name
                process = self.start(name)
                self.ready(name)
                # Act
                process.send_signal(signum)
                # Assert
                self.assertEqual(process.wait(timeout=10), 128 + signum)
                self.assert_released()

    def test_sigterm_reaches_a_foreground_wrapper_and_its_child(self) -> None:
        # Arrange
        child_code = """
import signal
import sys
import time
from pathlib import Path

name = sys.argv[1]

def stop(*args):
    Path(f'{name}.term').touch()
    while not Path(f'{name}.exit').exists():
        time.sleep(0.01)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
Path(f'{name}.ready').touch()
while not Path(f'{name}.exit').exists():
    time.sleep(0.01)
"""
        for close_fds in (True, False):
            with self.subTest(close_fds=close_fds):
                name = f"child-{close_fds}"
                wrapper = (
                    "import subprocess, sys; "
                    f"raise SystemExit(subprocess.call([sys.executable, '-c', "
                    f"{child_code!r}, sys.argv[1]], close_fds={close_fds}))"
                )
                process = self.start(name, wrapper)
                resume = self.root / f"{name}.exit"
                self.addCleanup(resume.touch)
                until((self.root / f"{name}.ready").exists)
                before_stop = self.events.read_text().count("container stop")

                # Act
                process.terminate()
                until((self.root / f"{name}.term").exists)
                # Assert: stop must wait for the child to finish.
                self.assertEqual(
                    self.events.read_text().count("container stop"), before_stop
                )
                self.assertIsNone(process.poll())
                # Act: let the signalled child finish.
                resume.touch()

                # Assert
                self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
                self.assert_released()

    def test_crash_before_group_registration_never_executes_the_task(self) -> None:
        # Arrange
        self.env["FAKE_GROUP_REGISTRATION"] = "1"
        process = self.start("unregistered")
        paused = self.root / "registration-paused"
        until(lambda: paused.exists() and paused.stat().st_size > 0)
        until((self.root / "bootstrap-ready").exists)
        token = next(iter(self.read_state()["leases"]))
        self.assertNotIn("task_group", self.read_state()["leases"][token])
        original = self.read_state()["generation"]

        # Act
        process.kill()
        process.wait(timeout=10)
        self.wait_for_task_guard(token)
        # Assert
        self.assertFalse((self.root / "unregistered.ready").exists())

        # Act: run another task against the released registry.
        del self.env["FAKE_GROUP_REGISTRATION"]
        task = self.start("registered")
        # Assert
        self.assertNotEqual(self.ready("registered")["generation"], original)
        self.finish(task, "registered")
        self.assertFalse((self.root / "unregistered.ready").exists())
        self.assert_released()

    def test_resistant_descendant_is_killed_while_unrelated_group_survives(
        self,
    ) -> None:
        # Arrange
        child = """
import signal
import time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('resistant-ready').touch()
while not Path('resistant-exit').exists():
    time.sleep(0.01)
"""
        wrapper = (
            "import subprocess, sys; "
            f"raise SystemExit(subprocess.call([sys.executable, '-c', {child!r}]))"
        )
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            process_group=0,
        )
        self.addCleanup(self.reap, unrelated)
        process = self.start("resistant", wrapper)
        self.addCleanup((self.root / "resistant-exit").touch)
        until((self.root / "resistant-ready").exists)
        self.assertNotIn("container stop", self.events.read_text())

        # Act
        process.terminate()

        # Assert
        self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
        self.assertIsNone(unrelated.poll())
        self.assert_released()

    def test_task_group_interrupt_with_exit_code_still_cleans_descendants(self) -> None:
        # Arrange
        child = """
import signal
import time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
Path('tty-child-ready').touch()
while not Path('tty-child-exit').exists():
    time.sleep(0.01)
"""
        wrapper = f"""
import signal
import subprocess
import sys

def finish(signum, frame):
    raise SystemExit(128 + signum)

signal.signal(signal.SIGINT, finish)
child = subprocess.Popen([sys.executable, '-c', {child!r}])
raise SystemExit(child.wait())
"""
        process = self.start("interrupt", wrapper)
        self.addCleanup((self.root / "tty-child-exit").touch)
        until((self.root / "tty-child-ready").exists)
        owner = next(iter(self.read_state()["leases"].values()))

        # Act
        os.killpg(owner["task_group"], signal.SIGINT)

        # Assert
        self.assertEqual(process.wait(timeout=10), 128 + signal.SIGINT)
        self.assert_released()

    def test_unknown_owner_birth_keeps_the_lease(self) -> None:
        # Arrange
        old = self.start("old")
        data = self.ready("old")
        token = next(iter(self.read_state()["leases"]))
        old.kill()
        old.wait(timeout=10)
        (self.root / "old.exit").touch()
        self.wait_for_task_guard(token)
        self.env["FAKE_UNKNOWN_PID"] = str(old.pid)

        # Act
        other = self.start("other")
        # Assert
        self.assertEqual(self.ready("other")["generation"], data["generation"])
        self.finish(other, "other")
        self.assertIn(token, self.read_state()["leases"])
        self.assertNotIn("container stop", self.events.read_text())

        # Act: retry after the process probe becomes reliable.
        del self.env["FAKE_UNKNOWN_PID"]
        new = self.start("new")
        # Assert
        self.assertNotEqual(self.ready("new")["generation"], data["generation"])
        self.finish(new, "new")
        self.assert_released()

    def test_ready_registry_write_failure_rolls_back_the_owned_stack(self) -> None:
        # Arrange
        self.env["FAKE_READY_WRITE"] = "1"

        # Act
        task = self.start("must-not-start")

        # Assert
        self.assertEqual(task.wait(timeout=10), 1)
        self.assertFalse((self.root / "must-not-start.ready").exists())
        self.assertIn("container stop", self.events.read_text())
        self.assert_released()

    def test_failed_stop_retains_ownership_for_later_recovery(self) -> None:
        # Arrange
        first = self.start("first")
        original = self.ready("first")
        problem = self.root / "fail-stop"
        problem.touch()
        # Act
        (self.root / "first.exit").touch()
        # Assert
        self.assertEqual(first.wait(timeout=10), 1)
        self.assertEqual(self.read_state()["phase"], "stopping")
        self.assertFalse(self.read_state()["leases"])

        # Act: retry with a responsive Docker fixture.
        problem.unlink()
        second = self.start("second")
        # Assert
        self.assertNotEqual(self.ready("second")["generation"], original["generation"])
        self.finish(second, "second")
        self.assert_released()

    def test_full_stack_refuses_an_active_db_only_lease(self) -> None:
        # Arrange
        database = self.start("database", mode="db")
        self.assertIsNone(self.ready("database")["url"])
        self.assertEqual(len(self.read_state()["containers"]), 1)

        # Act
        full = self.start("blocked")

        # Assert
        self.assertEqual(full.wait(timeout=10), 1)
        self.assertNotIn("container stop", self.events.read_text())
        self.finish(database, "database")
        self.assert_released()

    def test_last_release_rejects_replaced_container(self) -> None:
        # Arrange
        first = self.start("first")
        self.ready("first")
        records = cast(
            dict[str, dict[str, object]], json.loads(self.registry.read_text())
        )
        recorded_id = next(iter(records))
        replacement = records.pop(recorded_id)
        replacement["Id"] = "foreign-replacement"
        records["foreign-replacement"] = replacement
        self.registry.write_text(json.dumps(records))

        # Act
        (self.root / "first.exit").touch()
        # Assert
        self.assertEqual(first.wait(timeout=10), 1)
        self.assertTrue(self.state.exists())
        self.assertNotIn("container stop", self.events.read_text())
        self.assertEqual(json.loads(self.registry.read_text()), records)

    def test_unmanaged_and_one_off_containers_remain_untouched(self) -> None:
        # Arrange
        project = "nir-" + hashlib.sha256(os.fsencode(self.root)).hexdigest()[:16]
        containers = {
            name: {
                "Id": name,
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": project,
                        "com.docker.compose.service": name
                        if name != "one-off"
                        else "app",
                        "com.docker.compose.oneoff": "True"
                        if name == "one-off"
                        else "False",
                    }
                },
                "State": {"Running": True},
                "NetworkSettings": {
                    "Ports": {
                        "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8001"}],
                    }
                    if name == "app"
                    else {}
                },
            }
            for name in ("app", "db", "one-off")
        }
        self.registry.write_text(json.dumps(containers))

        # Act
        first = self.start("foreign")
        env = self.ready("foreign")
        # Assert
        self.assertEqual(env["project"], project)
        self.assertEqual(env["url"], "http://127.0.0.1:8001")
        self.assertEqual(env["generation"], "unmanaged")
        self.finish(first, "foreign")
        self.assertEqual(json.loads(self.registry.read_text()), containers)
        self.assertNotIn("container stop", self.events.read_text())
