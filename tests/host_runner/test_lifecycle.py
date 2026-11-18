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
RUN = """
import os
import sys
from pathlib import Path
from host_runner import leases
from host_runner.__main__ import run

def checked_birth(pid):
    if str(pid) == os.environ.get("FAKE_UNKNOWN_PID"):
        raise OSError("fixture process identity unavailable")
    return actual_birth(pid)

def checked_save(store, state):
    if state["phase"] == "ready" and os.environ.pop("FAKE_READY_WRITE", ""):
        raise OSError("fixture transient ready write failure")
    return actual_save(store, state)

actual_birth = leases.birth
actual_save = leases.LeaseStore.save
leases.birth = checked_birth
leases.LeaseStore.save = checked_save
raise SystemExit(run(Path.cwd(), sys.argv[2:], sys.argv[1] == 'db'))
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
        (self.root / "daemon").write_text("fixture-engine")
        self.events = self.root / "events"
        self.state = self.root / ".local/runtime/leases.json"
        docker = self.root / "docker"
        source = Path(__file__).with_name("fake_docker.py")
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
        self.volume_markers = [self.root / volume for volume in ("db", "app_data")]
        for marker in self.volume_markers:
            marker.write_text("persistent")

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
        group = self.read_state()["leases"][token]["task_group"]

        def unlocked() -> bool:
            if group_exists(group):
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
        self.assertTrue(
            all(path.read_text() == "persistent" for path in self.volume_markers)
        )

    def test_two_leases_keep_live_owner_with_expired_heartbeat(self) -> None:
        first = self.start("first")
        first_env = self.ready("first")
        original = self.read_state()
        owner = next(iter(original["leases"].values()))
        self.assertEqual(owner["birth"], birth(first.pid))
        owner["heartbeat"] = 0
        self.update_state(original)

        second = self.start("second")
        self.assertEqual(self.ready("second")["generation"], first_env["generation"])
        self.assertEqual(len(self.read_state()["leases"]), 2)
        self.assertEqual(self.read_state()["containers"], original["containers"])
        self.finish(second, "second")
        self.assertNotIn("container stop", self.events.read_text())

        self.finish(first, "first")
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

        new = self.start("new")
        self.assertNotEqual(self.ready("new")["generation"], data["generation"])
        self.finish(new, "new")
        self.assert_released()

    def test_sigkill_does_not_stop_a_surviving_task_then_recovers(self) -> None:
        old = self.start("old")
        data = self.ready("old")
        token = next(iter(self.read_state()["leases"]))
        self.addCleanup((self.root / "old.exit").touch)
        old.kill()
        old.wait(timeout=10)

        other = self.start("other")
        self.assertEqual(self.ready("other")["generation"], data["generation"])
        self.finish(other, "other")
        self.assertNotIn("container stop", self.events.read_text())

        (self.root / "old.exit").touch()
        self.wait_for_task_guard(token)
        new = self.start("new")
        self.assertNotEqual(self.ready("new")["generation"], data["generation"])
        self.finish(new, "new")
        self.assert_released()

    def test_acquire_waits_for_the_last_release_to_finish(self) -> None:
        old = self.start("old")
        first = self.ready("old")
        (self.root / "pause-stop").touch()
        self.addCleanup((self.root / "resume-stop").touch)
        (self.root / "old.exit").touch()
        until(lambda: "container stop" in self.events.read_text())
        self.assertEqual(self.read_state()["phase"], "stopping")

        new = self.start("new")
        # The contender process has reached the runner invocation, while Docker's stop is blocked.
        until(
            lambda: (
                new.poll() is not None
                or len(list(self.state.parent.glob("task-*.lock"))) == 1
            )
        )
        self.assertIsNone(new.poll())
        self.assertFalse((self.root / "new.ready").exists())
        (self.root / "resume-stop").touch()
        self.assertEqual(old.wait(timeout=10), 0)
        self.assertNotEqual(self.ready("new")["generation"], first["generation"])
        self.finish(new, "new")
        self.assert_released()

    def test_failed_task_and_handled_signals_release_the_stack(self) -> None:
        failure = self.start("failure", "raise SystemExit(17)")
        self.assertEqual(failure.wait(timeout=10), 17)
        self.assert_released()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                name = signum.name
                process = self.start(name)
                self.ready(name)
                process.send_signal(signum)
                self.assertEqual(process.wait(timeout=10), 128 + signum)
                self.assert_released()

    def test_sigterm_reaches_a_foreground_wrapper_and_its_child(self) -> None:
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

                process.terminate()
                until((self.root / f"{name}.term").exists)
                self.assertEqual(
                    self.events.read_text().count("container stop"), before_stop
                )
                self.assertIsNone(process.poll())
                resume.touch()

                self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
                self.assert_released()

    def test_unknown_owner_birth_keeps_the_lease(self) -> None:
        old = self.start("old")
        data = self.ready("old")
        token = next(iter(self.read_state()["leases"]))
        old.kill()
        old.wait(timeout=10)
        (self.root / "old.exit").touch()
        self.wait_for_task_guard(token)
        self.env["FAKE_UNKNOWN_PID"] = str(old.pid)

        other = self.start("other")
        self.assertEqual(self.ready("other")["generation"], data["generation"])
        self.finish(other, "other")
        self.assertIn(token, self.read_state()["leases"])
        self.assertNotIn("container stop", self.events.read_text())

        del self.env["FAKE_UNKNOWN_PID"]
        new = self.start("new")
        self.assertNotEqual(self.ready("new")["generation"], data["generation"])
        self.finish(new, "new")
        self.assert_released()

    def test_ready_registry_write_failure_rolls_back_the_owned_stack(self) -> None:
        self.env["FAKE_READY_WRITE"] = "1"

        task = self.start("must-not-start")

        self.assertEqual(task.wait(timeout=10), 1)
        self.assertFalse((self.root / "must-not-start.ready").exists())
        self.assertIn("container stop", self.events.read_text())
        self.assert_released()

    def test_failed_stop_retains_ownership_for_later_recovery(self) -> None:
        first = self.start("first")
        original = self.ready("first")
        problem = self.root / "fail-stop"
        problem.touch()
        (self.root / "first.exit").touch()
        self.assertEqual(first.wait(timeout=10), 1)
        self.assertEqual(self.read_state()["phase"], "stopping")
        self.assertFalse(self.read_state()["leases"])

        problem.unlink()
        second = self.start("second")
        self.assertNotEqual(self.ready("second")["generation"], original["generation"])
        self.finish(second, "second")
        self.assert_released()

    def test_db_only_and_port_fallback(self) -> None:
        (self.root / "conflict").touch()
        database = self.start("database", mode="db")
        self.assertIsNone(self.ready("database")["url"])
        self.assertEqual(len(self.read_state()["containers"]), 1)
        full = self.start("blocked")
        self.assertEqual(full.wait(timeout=10), 1)
        self.assertNotIn("container stop", self.events.read_text())
        self.finish(database, "database")

        fallback = self.start("fallback")
        self.assertEqual(self.ready("fallback")["url"], "http://127.0.0.1:47139")
        self.finish(fallback, "fallback")
        self.assert_released()

    def test_last_release_rejects_replaced_container(self) -> None:
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

        (self.root / "first.exit").touch()
        self.assertEqual(first.wait(timeout=10), 1)
        self.assertTrue(self.state.exists())
        self.assertNotIn("container stop", self.events.read_text())
        self.assertEqual(json.loads(self.registry.read_text()), records)

    def test_unmanaged_and_one_off_containers_remain_untouched(self) -> None:
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

        first = self.start("foreign")
        env = self.ready("foreign")
        self.assertEqual(env["project"], project)
        self.assertEqual(env["url"], "http://127.0.0.1:8001")
        self.assertEqual(env["generation"], "unmanaged")
        self.finish(first, "foreign")
        self.assertEqual(json.loads(self.registry.read_text()), containers)
        self.assertNotIn("container stop", self.events.read_text())
