"""Exercise journal failures across the actual launcher, runner PIPE, and PG."""

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

import django
from check_history import make_run
from django.db import connection

from collection import runner
from collection.selection import Direction, Selection, parse
from config import journal_maintenance
from config.collection_lock import RunSession
from config.control import encode, request
from config.journal import JOURNAL_ROOT
from config.journal_history import TERMINAL_SEAL, JournalView, read_run
from config.launcher import serve
from config.run_events import INSTANCE_ID_ENV, RUN_ID_ENV

START: dict[str, object] = {
    "action": "start",
    "selection": {
        "sources": ["cian"],
        "regions": [68],
        "categories": ["resale"],
        "deal_types": ["sale"],
    },
}


def until(check: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for journal fixture")
        time.sleep(0.02)


def document(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text()))


def observed_state(
    endpoint: Path, state: str, timeout: float = 10.0
) -> dict[str, object]:
    current: dict[str, object] = {}

    def matches() -> bool:
        current.update(request({"action": "status"}, endpoint))
        return current.get("state") == state

    until(matches, timeout)
    return current


def fixture_worker(root: Path, mode: str, selection: str) -> int:
    (root / "worker-pid").write_text(str(os.getpid()))

    def body(
        session: RunSession,
        _scope: Selection,
        stopped: Callable[[], bool],
        initialized: Callable[[], None],
    ) -> None:
        initialized()
        if mode == "overflow":
            until((root / "emit-flood").exists)
            for index in range(1025):
                if index == 1:
                    until((root / "sync-entered").exists)
                runner.emit(
                    "event",
                    "navigation_timeout",
                    source="cian",
                    stage="card",
                    level="warning",
                    publication_key="/detail/1906598",
                )
            (root / "flood-delivered").touch()
            while True:
                if stopped():
                    (root / "term-seen").touch()
                time.sleep(0.03)
        if mode in ("fail-before", "fail-after"):
            identifier = 191 if mode == "fail-before" else 192
            with session.transaction() as conn:
                conn.execute(
                    "INSERT INTO public.t09_probe (id) VALUES (%s)", (identifier,)
                )
            runner.emit("progress", committed_writes=1)
            (root / "committed").touch()
        if mode in (
            "invalid-schema",
            "invalid-run",
            "invalid-instance",
            "invalid-seq",
            "raw-reason",
            "nonmonotonic",
        ):
            runner.emit("progress", committed_writes=1)
            packet: dict[str, object] = {
                "event": "progress",
                "reason": "",
                "schema_version": 1,
                "run_id": os.environ[RUN_ID_ENV],
                "instance_id": os.environ[INSTANCE_ID_ENV],
                "seq": 4,
                "committed_writes": 2,
            }
            field, value = {
                "invalid-schema": ("schema_version", 8),
                "invalid-run": ("run_id", "wrong-run"),
                "invalid-instance": ("instance_id", "wrong-instance"),
                "invalid-seq": ("seq", 77),
                "raw-reason": ("reason", "fixture-secret-should-not-be-stored"),
                "nonmonotonic": ("committed_writes", 1),
            }[mode]
            packet[field] = value
            sys.stdout.buffer.write(encode(packet))
            sys.stdout.buffer.flush()
        if mode == "after-terminal":
            runner.emit("progress", committed_writes=1)
            runner.emit("finished", "completed")
            sys.stdout.buffer.write(b"{")
            sys.stdout.buffer.flush()
        if mode == "drain":
            until((root / "crash-now").exists)
            os._exit(17)
        if mode == "finish":
            until((root / "complete-body").exists)
        else:
            while not stopped():
                time.sleep(0.03)
        (root / "cleanup").touch()

    return runner.run(
        parse(json.loads(selection)),
        supported=lambda direction: (
            direction == Direction("cian", 68, "resale", "sale")
        ),
        body=body,
    )


def fixture_launcher(root: Path, mode: str) -> int:
    real_fsync = os.fsync
    real_replace = os.replace
    replaced_final = False
    replaced_seal = False
    real_read = read_run
    visits = 0

    def slow_history(path: Path, identifier: str, *, limit: int) -> JournalView:
        nonlocal visits
        if mode == "slow-retention" and visits < 2:
            visits += 1
            (root / f"retention-{visits}").touch()
            until((root / f"release-retention-{visits}").exists)
        return real_read(path, identifier, limit=limit)

    def replace(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal replaced_final, replaced_seal
        temporary = (
            Path(source)
            if src_dir_fd is None
            else Path(f"/proc/self/fd/{src_dir_fd}") / source
        )
        if (
            Path(target).name == "state.json"
            and document(temporary)["status"] == "finished"
        ):
            if mode == "fail-before":
                raise OSError("fixture-secret-should-not-be-stored")
            if mode == "fail-after":
                replaced_final = True
        real_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if mode == "fail-seal" and Path(target).name == TERMINAL_SEAL:
            replaced_seal = True

    def fsync(fd: int) -> None:
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if mode == "fail-after" and replaced_final and path.is_dir():
            raise OSError("fixture-secret-should-not-be-stored")
        if replaced_seal and path.is_dir():
            (root / "sync-entered").touch()
            until((root / "sync-release").exists)
            raise OSError("fixture-secret-should-not-be-stored")
        if path.name.startswith(".state.json-"):
            status = document(path)["status"]
            if (
                (mode.startswith("initial") and status == "starting")
                or (mode == "finish" and status == "finished")
                or (mode == "drain" and status == "unknown")
            ):
                (root / "sync-entered").touch()
                until((root / "sync-release").exists, 60)
        if (
            mode == "overflow"
            and path.name == "events.jsonl"
            and (root / "emit-flood").exists()
        ):
            (root / "sync-entered").touch()
            until((root / "sync-release").exists, 60)
        real_fsync(fd)

    web = [sys.executable, "-c", "import time; time.sleep(200)"]
    worker = [sys.executable, __file__, "worker", str(root), mode]
    with (
        patch.object(os, "fsync", fsync),
        patch.object(os, "replace", replace),
        patch.object(journal_maintenance, "read_run", slow_history),
    ):
        journal_root = JOURNAL_ROOT if mode == "initial-start" else root / "runs"
        return serve(
            root / "control.sock",
            web,
            worker,
            journal_root=journal_root,
            recover=mode in ("recover-existing", "slow-retention"),
        )


class JournalTests(unittest.TestCase):
    def fixture(
        self, mode: str, root: Path | None = None
    ) -> tuple[Path, Path, subprocess.Popen[bytes]]:
        if root is None:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            root = Path(temp.name)
        if mode == "slow-retention":
            journal_root = root / "runs"
            journal_root.mkdir()
            for order in range(11):
                make_run(journal_root, order=order)
        endpoint = root / "control.sock"
        process = subprocess.Popen(
            [sys.executable, __file__, "launcher", str(root), mode],
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.finish, process, root)
        until(endpoint.exists)
        return root, endpoint, process

    @staticmethod
    def finish(process: subprocess.Popen[bytes], root: Path) -> None:
        (root / "sync-release").touch()
        for index in (1, 2):
            (root / f"release-retention-{index}").touch()
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=48)
        finally:
            if process.stderr is not None:
                process.stderr.close()

    @staticmethod
    def notice(process: subprocess.Popen[bytes]) -> str:
        stream = process.stderr
        assert stream is not None
        if not select.select([stream], [], [], 2)[0]:
            raise AssertionError("Timed out waiting for journal fault notice")
        return stream.readline().decode().strip()

    def test_initial_ack_gates_spawn_and_stop_cancels_admission(self) -> None:
        for mode in (
            "initial-stop",
            "initial-late",
            "initial-shutdown",
            "initial-start",
        ):
            with self.subTest(mode=mode):
                # Arrange
                root, endpoint, process = self.fixture(mode)
                initial = request(START, endpoint)
                run_id = str(initial["run_id"])
                started = time.monotonic()
                until((root / "sync-entered").exists)
                before = request({"action": "status"}, endpoint)
                spawned_before_ack = (root / "worker-pid").exists()

                # Act
                if mode == "initial-shutdown":
                    process.terminate()
                elif mode != "initial-start":
                    request({"action": "stop", "run_id": run_id}, endpoint)
                if mode == "initial-late":
                    expired = observed_state(endpoint, "unknown")
                    fault_elapsed = time.monotonic() - started
                    fault_notice = self.notice(process)
                (root / "sync-release").touch()
                if mode == "initial-shutdown":
                    result = process.wait(timeout=8)
                    worker_spawned = (root / "worker-pid").exists()
                elif mode == "initial-start":
                    ready = observed_state(endpoint, "running")
                    run_directory = JOURNAL_ROOT / run_id
                    self.addCleanup(shutil.rmtree, run_directory)
                    durable_initial = (run_directory / "state.json").exists()
                    request({"action": "stop", "run_id": run_id}, endpoint)
                    stopped = observed_state(endpoint, "finished")
                elif mode == "initial-late":
                    path = root / "runs" / run_id / "state.json"
                    until(path.exists)
                    after = request({"action": "status"}, endpoint)
                else:
                    stopped = observed_state(endpoint, "finished")

                # Assert
                self.assertEqual(before["state"], "starting")
                self.assertFalse(spawned_before_ack)
                if mode == "initial-shutdown":
                    self.assertEqual(result, 0)
                    self.assertFalse(worker_spawned)
                elif mode == "initial-start":
                    self.assertIs(ready["storage_fault"], False)
                    self.assertTrue(durable_initial)
                    self.assertEqual(stopped["reason"], "stopped")
                elif mode == "initial-late":
                    self.assertEqual(expired["reason"], "journal_ack_timeout")
                    self.assertEqual(
                        fault_notice,
                        "Collection journal storage fault: journal_ack_timeout",
                    )
                    self.assertGreaterEqual(fault_elapsed, 4.65)
                    self.assertLess(fault_elapsed, 5.75)
                    self.assertIs(after["storage_fault"], True)
                    self.assertEqual(after["state"], "unknown")
                    self.assertFalse((root / "worker-pid").exists())
                else:
                    self.assertEqual(stopped["reason"], "stopped")
                    self.assertFalse((root / "worker-pid").exists())

    def test_final_ack_is_required_after_cleanup(self) -> None:
        from catalog.services import ReferenceDeleteBlocked, delete_references

        # Arrange
        root, endpoint, _ = self.fixture("finish")
        initial = request(START, endpoint)
        observed_state(endpoint, "running")
        run_id = str(initial["run_id"])

        # Act
        (root / "complete-body").touch()
        until((root / "sync-entered").exists)
        before = request({"action": "status"}, endpoint)
        late_stop = request({"action": "stop", "run_id": run_id}, endpoint)
        denied = request(START, endpoint)
        with self.assertRaises(ReferenceDeleteBlocked):
            delete_references("regions", [68], socket_path=endpoint)
        cleaned = (root / "cleanup").exists()
        (root / "sync-release").touch()
        final = observed_state(endpoint, "finished")
        next_run = request(START, endpoint)
        next_initial = root / "runs" / str(next_run["run_id"]) / "state.json"
        until(next_initial.exists)  # Earlier writes must have been processed FIFO.
        durable = document(root / "runs" / run_id / "state.json")

        # Assert
        self.assertTrue(cleaned)
        self.assertEqual(before["state"], "stopping")
        self.assertIs(before["cleanup_confirmed"], False)
        self.assertEqual(late_stop["state"], "stopping")
        self.assertIs(denied["ok"], False)
        self.assertIs(final["cleanup_confirmed"], True)
        self.assertEqual(durable["status"], "finished")
        self.assertEqual(durable["outcome"], "completed")
        self.assertIs(durable["cleanup_confirmed"], True)

    def test_before_and_after_rename_errors_never_ack_or_rollback_pg(self) -> None:
        for mode, persisted, identifier in (
            ("fail-before", "stopping", 191),
            ("fail-after", "finished", 192),
        ):
            with self.subTest(mode=mode):
                # Arrange
                root, endpoint, process = self.fixture(mode)
                initial = request(START, endpoint)
                observed_state(endpoint, "running")
                until((root / "committed").exists)
                run_id = str(initial["run_id"])

                # Act
                request({"action": "stop", "run_id": run_id}, endpoint)
                failure = observed_state(endpoint, "unknown")
                fault_notice = self.notice(process)
                run = root / "runs" / run_id
                snapshot = document(run / "state.json")
                events = (run / "events.jsonl").read_text()
                unsealed = not (run / TERMINAL_SEAL).exists()
                view = read_run(root / "runs", run_id)
                if mode == "fail-after":
                    process.terminate()
                    process.wait(timeout=8)
                    _, recovered_endpoint, _ = self.fixture("recover-existing", root)
                    observed_state(recovered_endpoint, "idle")
                    recovered = read_run(root / "runs", run_id)
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM public.t09_probe WHERE id=%s", (identifier,)
                    )
                    row = cast(tuple[int] | None, cursor.fetchone())

                # Assert
                self.assertEqual(failure["reason"], "journal_write_failed")
                self.assertEqual(
                    fault_notice,
                    "Collection journal storage fault: journal_write_failed",
                )
                self.assertIs(failure["cleanup_confirmed"], False)
                self.assertEqual(snapshot["status"], persisted)
                self.assertTrue(unsealed)
                if mode == "fail-after":
                    assert view.snapshot is not None and recovered.snapshot is not None
                    self.assertEqual(view.snapshot["status"], "unknown")
                    self.assertIs(view.snapshot["cleanup_confirmed"], False)
                    self.assertEqual(recovered.snapshot["outcome"], "error")
                    self.assertEqual(recovered.snapshot["reason"], "app_restarted")
                self.assertEqual(row, (identifier,))
                self.assertNotIn("fixture-secret", events)
                self.assertIn('"committed_writes":1', events)

    def test_snapshot_ack_precedes_failing_seal_and_survives_restart(self) -> None:
        # Arrange
        root, endpoint, process = self.fixture("fail-seal")
        initial = request(START, endpoint)
        observed_state(endpoint, "running")
        run_id = str(initial["run_id"])
        run = root / "runs" / run_id

        # Act
        request({"action": "stop", "run_id": run_id}, endpoint)
        until((root / "sync-entered").exists)
        acknowledged = observed_state(endpoint, "finished")
        sealed = (run / TERMINAL_SEAL).exists()
        prior = (run / "state.json").read_bytes()
        (root / "sync-release").touch()
        notice = self.notice(process)
        denied = request(START, endpoint)
        process.terminate()
        exit_code = process.wait(timeout=8)
        _, recovered_endpoint, _ = self.fixture("recover-existing", root)
        observed_state(recovered_endpoint, "idle")
        recovered = read_run(root / "runs", run_id)

        # Assert
        self.assertIs(acknowledged["cleanup_confirmed"], True)
        self.assertTrue(sealed)
        self.assertEqual(
            notice, "Collection journal storage fault: journal_seal_failed"
        )
        self.assertEqual(denied["state"], "finished")
        self.assertIs(denied["ok"], False)
        self.assertIs(denied["cleanup_confirmed"], False)
        self.assertEqual(exit_code, 1)
        self.assertEqual((run / "state.json").read_bytes(), prior)
        assert recovered.snapshot is not None
        self.assertEqual(recovered.snapshot["outcome"], "stopped")

    def test_full_queue_drains_and_stop_kill_do_not_wait_for_blocked_fsync(
        self,
    ) -> None:
        # Arrange
        root, endpoint, process = self.fixture("overflow")
        initial = request(START, endpoint)
        observed_state(endpoint, "running")
        # Let normal lifecycle snapshots finish before the controlled flood.
        run_id = str(initial["run_id"])
        event_file = root / "runs" / run_id / "events.jsonl"
        until(lambda: '"event":"ready"' in event_file.read_text())
        worker = Path(f"/proc/{(root / 'worker-pid').read_text()}")

        # Act
        started = time.monotonic()
        (root / "emit-flood").touch()
        until((root / "sync-entered").exists)
        fault = observed_state(endpoint, "unknown")
        fault_notice = self.notice(process)
        until((root / "term-seen").exists)
        until((root / "flood-delivered").exists)
        repeated = request({"action": "stop", "run_id": run_id}, endpoint)
        until(
            lambda: not worker.exists(),
            timeout=max(0.0, started + 45 - time.monotonic()),
        )
        forced_elapsed = time.monotonic() - started
        while_blocked = request({"action": "status"}, endpoint)
        app_stop = time.monotonic()
        process.terminate()
        app_code = process.wait(timeout=3)
        app_elapsed = time.monotonic() - app_stop
        assert process.stderr is not None
        remaining_stderr = process.stderr.read()
        print(
            f"T11 blocked fsync: runner reaped at {forced_elapsed:.3f}s; launcher shutdown {app_elapsed:.3f}s"
        )

        # Assert
        self.assertEqual(fault["reason"], "journal_queue_full")
        self.assertEqual(
            fault_notice, "Collection journal storage fault: journal_queue_full"
        )
        self.assertEqual(remaining_stderr, b"")
        self.assertIs(repeated["ok"], True)
        self.assertGreaterEqual(forced_elapsed, 39.5)
        self.assertLess(forced_elapsed, 45)
        self.assertIs(while_blocked["storage_fault"], True)
        self.assertGreater(cast(int, while_blocked["lost_events"]), 0)
        self.assertIs(while_blocked["cleanup_confirmed"], False)
        self.assertFalse((root / "sync-release").exists())
        self.assertNotEqual(app_code, 0)
        self.assertLess(app_elapsed, 3)

    def test_shutdown_drains_an_accepted_healthy_unknown_snapshot(self) -> None:
        # Arrange
        root, endpoint, process = self.fixture("drain")
        initial = request(START, endpoint)
        observed_state(endpoint, "running")
        path = root / "runs" / str(initial["run_id"]) / "state.json"

        # Act
        (root / "crash-now").touch()
        until((root / "sync-entered").exists)
        unknown = observed_state(endpoint, "unknown")
        process.terminate()
        until(lambda: not endpoint.exists())
        # The old shutdown abandoned writes after 0.15 s.
        time.sleep(0.3)
        waiting_for_snapshot = process.poll() is None
        (root / "sync-release").touch()
        code = process.wait(timeout=3)
        saved = document(path)

        # Assert
        self.assertIs(unknown["storage_fault"], False)
        self.assertTrue(waiting_for_snapshot)
        self.assertEqual(code, 0)
        self.assertEqual(saved["status"], "unknown")
        self.assertIs(saved["cleanup_confirmed"], False)

    def test_start_and_ack_do_not_wait_for_a_retention_scan(self) -> None:
        # Arrange
        root, endpoint, _ = self.fixture("slow-retention")
        until((root / "retention-1").exists)
        idle = observed_state(endpoint, "idle")

        # Act
        queued = request(START, endpoint)
        (root / "release-retention-1").touch()
        until((root / "retention-2").exists)
        running = observed_state(endpoint, "running")
        still_scanning = not (root / "release-retention-2").exists()
        (root / "release-retention-2").touch()
        request({"action": "stop", "run_id": running["run_id"]}, endpoint)
        finished = observed_state(endpoint, "finished")

        # Assert
        self.assertIs(idle["cleanup_confirmed"], True)
        self.assertEqual(queued["state"], "starting")
        self.assertTrue(still_scanning)
        self.assertEqual(running["run_id"], queued["run_id"])
        self.assertIs(finished["cleanup_confirmed"], True)

    def test_reader_validates_identity_sequence_and_safe_payload_before_disk(
        self,
    ) -> None:
        modes = (
            "invalid-schema",
            "invalid-run",
            "invalid-instance",
            "invalid-seq",
            "raw-reason",
            "nonmonotonic",
            "after-terminal",
        )
        for mode in modes:
            with self.subTest(mode=mode):
                # Arrange
                root, endpoint, _ = self.fixture(mode)

                # Act
                initial = request(START, endpoint)
                failure = observed_state(endpoint, "unknown")
                path = root / "runs" / str(initial["run_id"]) / "events.jsonl"

                def progress_recorded(path: Path = path) -> bool:
                    return '"event":"progress"' in path.read_text()

                until(progress_recorded)
                recorded = [json.loads(line) for line in path.read_text().splitlines()]

                # Assert
                self.assertEqual(failure["reason"], "invalid_runner_event")
                expected = [1, 2, 3, 4] if mode == "after-terminal" else [1, 2, 3]
                self.assertEqual([event["seq"] for event in recorded], expected)
                self.assertNotIn("fixture-secret", json.dumps(recorded))


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if len(sys.argv) > 1 and sys.argv[1] == "launcher":
        raise SystemExit(fixture_launcher(Path(sys.argv[2]), sys.argv[3]))
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        raise SystemExit(fixture_worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4]))
    django.setup()
    unittest.main(verbosity=2)
