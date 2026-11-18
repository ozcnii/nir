"""Exercise the live Unix socket, Linux children and PostgreSQL interlock."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from unittest.mock import patch

import django
from django.db import connection, transaction

from config.collection_lock import RunSession, try_delete_lock
from config.control import MAX_MESSAGE, encode, receive, request

SELECTION: dict[str, object] = {
    "sources": ["cian"],
    "regions": [68],
    "categories": ["resale"],
    "deal_types": ["sale"],
}
START: dict[str, object] = {"action": "start", "selection": SELECTION}


def eventually(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 7
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for the controlled scenario")
        time.sleep(0.03)


def fixture_child(root: Path, snapshot: str) -> int:
    from collection.runner import idle_run, run
    from collection.selection import Direction, Selection, parse

    (root / "child-executed").touch()
    if (root / "wait-before-armed").exists():
        (root / "before-armed").touch()
        eventually((root / "continue-to-armed").exists)
    django.setup()

    def supported(direction: Direction) -> bool:
        if (root / "hold-initialization").exists():
            (root / "initializing").touch()
            eventually((root / "continue-initialization").exists)
        return direction == Direction("cian", 68, "resale", "sale")

    def body(
        session: RunSession, selection: Selection, stopped: Callable[[], bool]
    ) -> None:
        if (root / "probe-write").exists():
            (root / "write-gate").touch()
            eventually((root / "attempt-write").exists)
            (root / "attempted-write").touch()
            with session.transaction() as connection:
                connection.execute("INSERT INTO public.t09_probe (id) VALUES (1)")
            (root / "write-completed").touch()
        idle_run(session, selection, stopped)

    return run(parse(json.loads(snapshot)), supported=supported, body=body)


def fixture_launcher(root: Path) -> int:
    from config import launcher

    def monitored_receive(
        connection: socket.socket, deadline: float
    ) -> dict[str, object]:
        if (root / "observe-receive").exists():
            (root / "receiving").touch()
        return receive(connection, deadline)

    web = [
        sys.executable,
        "-c",
        "import os, sys, time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getppid())); time.sleep(1000)",
        str(root / "web-parent"),
    ]
    runner = [sys.executable, __file__, "child", str(root)]
    with patch.object(launcher, "receive", monitored_receive):
        return launcher.serve(root / "control.sock", web, runner)


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.endpoint = self.root / "control.sock"

    def start_fixture(self) -> subprocess.Popen[bytes]:
        parent = subprocess.Popen(
            [sys.executable, __file__, "launcher", str(self.root)]
        )
        self.addCleanup(self.finish_fixture, parent)
        eventually(self.endpoint.exists)
        return parent

    @staticmethod
    def finish_fixture(parent: subprocess.Popen[bytes]) -> None:
        if parent.poll() is None:
            parent.send_signal(signal.SIGTERM)
        if parent.wait(timeout=12) != 0:
            raise AssertionError("Controlled launcher failed")

    def control(self, command: dict[str, object]) -> dict[str, object]:
        return request(command, self.endpoint)

    def status(self, expected: str) -> dict[str, object]:
        current: dict[str, object] = {}

        def reached() -> bool:
            current.update(self.control({"action": "status"}))
            return current.get("state") == expected

        eventually(reached)
        return current

    def backend(self) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pid FROM pg_stat_activity WHERE application_name = 'nir-collection'"
            )
            rows = cast(list[tuple[int]], cursor.fetchall())
        self.assertEqual(len(rows), 1)
        return rows[0][0]

    def test_missing_pidfd_keeps_executable_behind_the_gate(self) -> None:
        from config.launcher import Child

        # Arrange
        marker = self.root / "executed"
        command = [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ]

        # Act
        with patch(
            "config.launcher.open_pidfd", side_effect=OSError("fixture pidfd failure")
        ):
            child = Child.spawn(command, permitted=lambda: True)
        try:
            code = child.process.wait(timeout=4)
        finally:
            child.close()

        # Assert
        self.assertEqual(code, 125)
        self.assertFalse(marker.exists())

    def test_shutdown_refuses_start_already_partial_on_the_socket(self) -> None:
        # Arrange
        (self.root / "observe-receive").touch()
        launcher = self.start_fixture()
        raw = encode(START)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.endpoint))
            client.sendall(raw[:8])
            eventually((self.root / "receiving").exists)

            # Act
            launcher.send_signal(signal.SIGTERM)
            client.sendall(raw[8:])
            answer = receive(client, time.monotonic() + 3)
        code = launcher.wait(timeout=4)

        # Assert
        self.assertEqual(code, 0)
        self.assertIs(answer["ok"], False)
        self.assertFalse((self.root / "child-executed").exists())

    def test_invalid_output_is_discarded_without_preventing_reap(self) -> None:
        from config.launcher import Supervisor

        # Arrange
        marker = self.root / "wrote-all-output"
        command = [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "sys.stdout.buffer.write(b'x' * 65536); sys.stdout.buffer.flush(); "
            f"Path({str(marker)!r}).touch()",
        ]
        supervisor = Supervisor(command)
        peak = 0

        def reaped() -> bool:
            nonlocal peak
            supervisor.tick()
            if supervisor.current is None:
                return True
            peak = max(peak, len(supervisor.current.buffer))
            return False

        # Act
        supervisor.dispatch(START)
        eventually(reaped)
        result = supervisor.status()

        # Assert
        self.assertLess(peak, MAX_MESSAGE)
        self.assertTrue(marker.exists())
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["reason"], "invalid_runner_event")

    def test_stop_during_pre_armed_initialization_waits_for_signal_handler(
        self,
    ) -> None:
        # Arrange
        (self.root / "wait-before-armed").touch()
        self.start_fixture()
        initial = self.control(START)
        eventually((self.root / "before-armed").exists)

        # Act
        stopping = self.control({"action": "stop", "run_id": initial["run_id"]})
        (self.root / "continue-to-armed").touch()
        result = self.status("finished")

        # Assert
        self.assertEqual(stopping["state"], "stopping")
        self.assertEqual(result["reason"], "stopped")
        self.assertIs(result["cleanup_confirmed"], True)

    def test_concurrent_start_pending_ready_and_stop(self) -> None:
        from catalog.services import ReferenceDeleteBlocked, delete_references

        # Arrange
        (self.root / "hold-initialization").touch()
        launcher = self.start_fixture()
        eventually(
            lambda: (
                (self.root / "web-parent").exists()
                and (self.root / "web-parent").stat().st_size > 0
            )
        )
        # Act
        with ThreadPoolExecutor(max_workers=2) as requests:
            answers = list(requests.map(self.control, (START, START)))
        pending = self.status("starting")
        try:
            eventually((self.root / "initializing").exists)
            self.backend()
            with self.assertRaises(ReferenceDeleteBlocked):
                delete_references("regions", (68,), socket_path=self.endpoint)
        finally:
            (self.root / "continue-initialization").touch()
        running = self.status("running")
        stale_stop = self.control({"action": "stop", "run_id": "not-the-current-run"})
        unsupported_command = self.control({"action": "status", "pid": launcher.pid})
        stopped = self.control({"action": "stop", "run_id": running["run_id"]})
        finished = self.status("finished")

        # Assert
        self.assertEqual(int((self.root / "web-parent").read_text()), launcher.pid)
        self.assertEqual(self.endpoint.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sum(answer["ok"] is True for answer in answers), 1)
        self.assertIs(pending["cleanup_confirmed"], False)
        self.assertEqual(running["run_id"], pending["run_id"])
        self.assertIs(stale_stop["ok"], False)
        self.assertIs(unsupported_command["ok"], False)
        self.assertEqual(stopped["state"], "stopping")
        self.assertIs(finished["cleanup_confirmed"], True)

    def test_delete_winner_prevents_start_and_missing_region_is_revalidated(
        self,
    ) -> None:
        from catalog.models import Region
        from catalog.services import delete_references

        # Arrange
        self.start_fixture()
        original = Region.objects.get(pk=68).name
        self.addCleanup(
            Region.objects.update_or_create, pk=68, defaults={"name": original}
        )
        self.status("idle")

        # Act
        with transaction.atomic():
            deleted = delete_references("regions", (68,), socket_path=self.endpoint)
            first = self.control(START)
            refused = self.status("finished")

        # Assert
        self.assertEqual(deleted, 1)
        self.assertEqual(first["state"], "starting")
        self.assertEqual(refused["reason"], "lock_busy")
        self.assertFalse(Region.objects.filter(pk=68).exists())

        # Act
        self.control(START)
        missing = self.status("finished")

        # Assert
        self.assertEqual(missing["reason"], "unsupported_or_missing_region")

    def test_lock_loss_stops_write_and_fences_start_and_delete(self) -> None:
        from catalog.services import ReferenceDeleteBlocked, delete_references

        # Arrange
        (self.root / "probe-write").touch()
        self.start_fixture()
        self.control(START)
        self.status("running")
        eventually((self.root / "write-gate").exists)
        backend = self.backend()

        # Act
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(%s, 5000)", (backend,))
            termination = cast(tuple[bool] | None, cursor.fetchone())
        (self.root / "attempt-write").touch()
        result = self.status("unknown")
        second_start = self.control(START)
        with transaction.atomic():
            key_is_free = try_delete_lock()
        with self.assertRaises(ReferenceDeleteBlocked):
            delete_references("regions", (68,), socket_path=self.endpoint)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM public.t09_probe")
            writes = cast(tuple[int] | None, cursor.fetchone())

        # Assert
        self.assertEqual(termination, (True,))
        self.assertEqual(result["reason"], "lock_lost")
        self.assertIs(result["cleanup_confirmed"], False)
        self.assertTrue((self.root / "attempted-write").exists())
        self.assertFalse((self.root / "write-completed").exists())
        self.assertIs(second_start["ok"], False)
        self.assertTrue(key_is_free)
        self.assertEqual(writes, (0,))

    def test_unavailable_or_unresponsive_socket_refuses_deletion(self) -> None:
        from catalog.models import Region
        from catalog.services import ReferenceDeleteBlocked, delete_references

        # Arrange
        current = Region.objects.get(pk=68)
        absent = self.root / "missing.sock"

        # Act
        with self.assertRaises(ReferenceDeleteBlocked):
            delete_references("regions", (current.pk,), socket_path=absent)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unresponsive:
            unresponsive.bind(str(self.endpoint))
            unresponsive.listen(1)
            start = time.monotonic()
            with self.assertRaises(ReferenceDeleteBlocked):
                delete_references("regions", (current.pk,), socket_path=self.endpoint)
            elapsed = time.monotonic() - start

        # Assert
        self.assertLess(elapsed, 1)
        self.assertTrue(Region.objects.filter(pk=current.pk).exists())

    def test_unsupported_direction_and_disabled_sources_never_report_running(
        self,
    ) -> None:
        from catalog.models import PropertyType, Source
        from catalog.services import delete_references

        # Arrange
        self.start_fixture()
        unsupported = {**SELECTION, "deal_types": ["day_rent"]}

        # Act
        self.control({"action": "start", "selection": unsupported})
        invalid_route = self.status("finished")

        # Assert
        self.assertEqual(invalid_route["reason"], "unsupported_direction")

        # Arrange
        label = PropertyType.objects.get(pk=1).name
        self.addCleanup(
            PropertyType.objects.update_or_create, pk=1, defaults={"name": label}
        )

        # Act
        deleted = delete_references("property_types", (1,), socket_path=self.endpoint)
        self.control(START)
        invalid_type = self.status("finished")

        # Assert
        self.assertEqual(deleted, 1)
        self.assertEqual(invalid_type["reason"], "unsupported_or_missing_property_type")

        # Arrange
        PropertyType.objects.create(pk=1, name=label)
        Source.objects.filter(pk="cian").update(enabled=False)
        self.addCleanup(Source.objects.filter(pk="cian").update, enabled=True)

        # Act
        self.control(START)
        disabled = self.status("finished")

        # Assert
        self.assertEqual(disabled["reason"], "unsupported_or_disabled_source")


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if len(sys.argv) > 1:
        if sys.argv[1] == "launcher":
            raise SystemExit(fixture_launcher(Path(sys.argv[2])))
        if sys.argv[1] == "child":
            raise SystemExit(fixture_child(Path(sys.argv[2]), sys.argv[3]))
    django.setup()
    unittest.main(verbosity=2)
