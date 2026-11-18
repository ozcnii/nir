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

import django
from django.db import connection, transaction

from config.collection_lock import RunSession, try_delete_lock
from config.control import request

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
    from config.launcher import serve

    web = [
        sys.executable,
        "-c",
        "import os, sys, time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getppid())); time.sleep(1000)",
        str(root / "web-parent"),
    ]
    runner = [sys.executable, __file__, "child", str(root)]
    return serve(root / "control.sock", web, runner)


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
        with ThreadPoolExecutor(max_workers=2) as requests:
            # Act
            answers = list(requests.map(self.control, (START, START)))

        # Assert
        self.assertEqual(int((self.root / "web-parent").read_text()), launcher.pid)
        self.assertEqual(self.endpoint.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sum(answer["ok"] is True for answer in answers), 1)
        pending = self.status("starting")
        self.assertIs(pending["cleanup_confirmed"], False)
        try:
            eventually((self.root / "initializing").exists)
            self.backend()
            with self.assertRaises(ReferenceDeleteBlocked):
                delete_references("regions", (68,), socket_path=self.endpoint)
        finally:
            (self.root / "continue-initialization").touch()
        running = self.status("running")
        self.assertEqual(running["run_id"], pending["run_id"])
        self.assertIs(
            self.control({"action": "stop", "run_id": "not-the-current-run"})["ok"],
            False,
        )
        self.assertIs(
            self.control({"action": "status", "pid": launcher.pid})["ok"], False
        )

        # Act
        stopped = self.control({"action": "stop", "run_id": running["run_id"]})

        # Assert
        self.assertEqual(stopped["state"], "stopping")
        self.assertIs(self.status("finished")["cleanup_confirmed"], True)

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
            self.assertEqual(
                delete_references("regions", (68,), socket_path=self.endpoint), 1
            )
            first = self.control(START)
            refused = self.status("finished")

        # Assert
        self.assertEqual(first["state"], "starting")
        self.assertEqual(refused["reason"], "lock_busy")
        self.assertFalse(Region.objects.filter(pk=68).exists())

        # Act
        self.control(START)

        # Assert
        self.assertEqual(
            self.status("finished")["reason"], "unsupported_or_missing_region"
        )

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
            self.assertEqual(cursor.fetchone(), (True,))
        (self.root / "attempt-write").touch()
        result = self.status("unknown")

        # Assert
        self.assertEqual(result["reason"], "lock_lost")
        self.assertIs(result["cleanup_confirmed"], False)
        self.assertTrue((self.root / "attempted-write").exists())
        self.assertFalse((self.root / "write-completed").exists())
        self.assertIs(self.control(START)["ok"], False)
        with transaction.atomic():
            self.assertTrue(try_delete_lock())
        with self.assertRaises(ReferenceDeleteBlocked):
            delete_references("regions", (68,), socket_path=self.endpoint)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM public.t09_probe")
            self.assertEqual(cursor.fetchone(), (0,))

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

        # Assert
        self.assertEqual(self.status("finished")["reason"], "unsupported_direction")

        # Arrange
        label = PropertyType.objects.get(pk=1).name
        self.addCleanup(
            PropertyType.objects.update_or_create, pk=1, defaults={"name": label}
        )

        # Act
        self.assertEqual(
            delete_references("property_types", (1,), socket_path=self.endpoint), 1
        )
        self.control(START)

        # Assert
        self.assertEqual(
            self.status("finished")["reason"], "unsupported_or_missing_property_type"
        )

        # Arrange
        PropertyType.objects.create(pk=1, name=label)
        Source.objects.filter(pk="cian").update(enabled=False)
        self.addCleanup(Source.objects.filter(pk="cian").update, enabled=True)

        # Act
        self.control(START)

        # Assert
        self.assertEqual(
            self.status("finished")["reason"], "unsupported_or_disabled_source"
        )


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if len(sys.argv) > 1:
        if sys.argv[1] == "launcher":
            raise SystemExit(fixture_launcher(Path(sys.argv[2])))
        if sys.argv[1] == "child":
            raise SystemExit(fixture_child(Path(sys.argv[2]), sys.argv[3]))
    django.setup()
    unittest.main(verbosity=2)
