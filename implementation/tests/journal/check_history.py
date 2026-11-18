"""Real filesystem evidence for rotation, retention and bounded history reads."""

import json
import tempfile
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from runtime import journal_history
from runtime.control import encode
from runtime.journal import write_file, write_snapshot
from runtime.journal_history import (
    EVENT_FILES,
    SEGMENT_LIMIT,
    TERMINAL_SEAL,
    JournalView,
    directory,
    read_run,
    terminal_digest,
)
from runtime.journal_maintenance import append_event, retention_steps
from runtime.run_events import journal_record

INSTANCE = "a" * 32
SELECTION = {
    "sources": ["cian"],
    "regions": [68],
    "categories": ["resale"],
    "deal_types": ["sale"],
}
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_run(
    root: Path, *, order: int = 0, status: str = "finished", confirmed: bool = True
) -> tuple[str, Path]:
    identifier = uuid4().hex
    run = root / identifier
    run.mkdir()
    (run / "events.jsonl").touch()
    instant = (BASE + timedelta(minutes=order)).isoformat()
    finished = status == "finished"
    record: dict[str, object] = {
        "schema_version": 1,
        "run_id": identifier,
        "instance_id": INSTANCE,
        "status": status,
        "outcome": "completed" if finished else None,
        "reason": "completed" if finished else "",
        "started_at": instant,
        "updated_at": instant,
        "finished_at": instant if finished else None,
        "selection": SELECTION,
        "progress": {"committed_writes": 2},
        "cleanup_confirmed": finished,
    }
    with directory(run) as fd:
        write_snapshot(fd, record)
        if finished and confirmed:
            write_file(fd, TERMINAL_SEAL, terminal_digest(record))
    return identifier, run


def record(identifier: str, seq: int) -> dict[str, object]:
    return journal_record(
        {
            "schema_version": 1,
            "run_id": identifier,
            "instance_id": INSTANCE,
            "seq": seq,
            "event": "event",
            "source": "cian",
            "stage": "card",
            "level": "warning",
            "publication_key": "/detail/1906598",
            "reason": "navigation_timeout",
        }
    )


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_rotation_at_real_limit_keeps_two_old_segments(self) -> None:
        # Arrange
        identifier, run = make_run(self.root)
        sequence = 1

        # Act
        with directory(run) as fd:
            for _ in range(3):
                size = (run / "events.jsonl").stat().st_size
                with (run / "events.jsonl").open("ab") as stream:
                    while (
                        size + len(line := encode(record(identifier, sequence)))
                    ) <= SEGMENT_LIMIT:
                        stream.write(line)
                        size += len(line)
                        sequence += 1
                append_event(fd, record(identifier, sequence))
                sequence += 1
        view = read_run(self.root, identifier, limit=2)

        # Assert
        self.assertTrue(
            all(
                0 < (run / name).stat().st_size <= SEGMENT_LIMIT for name in EVENT_FILES
            )
        )
        self.assertEqual(view.kind, "ok")
        self.assertTrue(view.prefix_lost)
        self.assertEqual(
            [entry["seq"] for entry in view.events], [sequence - 2, sequence - 1]
        )

    def test_empty_partial_central_corruption_and_unreadable_are_distinct(self) -> None:
        # Arrange
        identifier, run = make_run(self.root)
        events = run / "events.jsonl"
        state = run / "state.json"

        # Act
        empty = read_run(self.root, identifier)
        events.write_bytes(encode(record(identifier, 1)) + b'{"interrupted"')
        partial = read_run(self.root, identifier)
        events.write_bytes(
            encode(record(identifier, 1))
            + b"{invalid}\n"
            + encode(record(identifier, 3))
        )
        corrupt = read_run(self.root, identifier)
        state.write_bytes(b"invalid")
        snapshot_corrupt = read_run(self.root, identifier)
        state.unlink()
        unreadable = read_run(self.root, identifier)

        # Assert
        self.assertEqual(empty.kind, "empty")
        self.assertEqual(partial.kind, "partial")
        self.assertEqual([row["seq"] for row in partial.events], [1])
        self.assertEqual(corrupt.kind, "corrupt")
        self.assertEqual(snapshot_corrupt.kind, "snapshot_corrupt")
        self.assertEqual(unreadable.kind, "snapshot_unreadable")

    def test_retention_keeps_ten_and_does_not_hide_a_concurrent_change(self) -> None:
        # Arrange
        healthy = [make_run(self.root, order=index) for index in range(11)]
        current, current_path = make_run(self.root, order=-100)
        unconfirmed_id, unconfirmed = make_run(self.root, order=-30, confirmed=False)
        mismatched_id, mismatched = make_run(self.root, order=-25)
        (mismatched / TERMINAL_SEAL).write_bytes(b"x" * 65)
        _, running = make_run(self.root, status="running")
        _, unknown = make_run(self.root, status="unknown")
        _, bad_state = make_run(self.root, order=-20)
        (bad_state / "state.json").write_text("{invalid")
        corrupt_id, corrupt_log = make_run(self.root, order=-5)
        (corrupt_log / "events.jsonl").write_bytes(
            encode(record(corrupt_id, 1)) + b"{bad}\n"
        )
        _, unsafe = make_run(self.root, order=-3)
        target = self.root / "foreign"
        target.write_text("preserve")
        (unsafe / "events.jsonl").unlink()
        (unsafe / "events.jsonl").symlink_to(target)
        link = self.root / uuid4().hex
        link.symlink_to(target)
        old_id, old = healthy[0]

        # Act
        def retain() -> None:
            for _ in retention_steps(self.root, current):
                pass

        view = self.read_during(old_id, retain)
        tentative = read_run(self.root, unconfirmed_id)
        incorrect_seal = read_run(self.root, mismatched_id)

        # Assert
        self.assertEqual(view.kind, "changed")
        self.assertEqual(read_run(self.root, old_id).kind, "missing")
        self.assertFalse(old.exists())
        self.assertTrue(all(path.exists() for _, path in healthy[1:]))
        for path in (
            current_path,
            running,
            unknown,
            bad_state,
            corrupt_log,
            unsafe,
            link,
        ):
            self.assertTrue(path.exists())
        self.assertEqual(target.read_text(), "preserve")
        for history in (tentative, incorrect_seal):
            self.assertEqual(history.kind, "partial")
            assert history.snapshot is not None
            self.assertEqual(history.snapshot["status"], "unknown")
            self.assertIs(history.snapshot["cleanup_confirmed"], False)

    def test_a_read_during_rotation_reports_the_change(self) -> None:
        # Arrange
        identifier, run = make_run(self.root, status="running")
        active = run / "events.jsonl"
        # An oversized fixture forces the rotation branch before the reader resumes.
        with active.open("wb") as stream:
            stream.truncate(SEGMENT_LIMIT)

        def rotate() -> None:
            with directory(run) as fd:
                append_event(fd, record(identifier, 2))

        # Act
        view = self.read_during(identifier, rotate)

        # Assert
        self.assertEqual(view.kind, "changed")
        self.assertTrue((run / "events.jsonl.1").exists())
        latest = json.loads(active.read_text())
        self.assertEqual(latest["seq"], 2)

    def read_during(self, identifier: str, mutate: Callable[[], None]) -> JournalView:
        reading = threading.Event()
        release = threading.Event()
        original = journal_history.read_events
        parent = threading.current_thread()

        def delayed(
            run: int, names: tuple[str, ...], snapshot: dict[str, object], limit: int
        ) -> JournalView:
            if threading.current_thread() is not parent:
                reading.set()
                if not release.wait(5):
                    raise AssertionError("Reader change fixture did not release")
            return original(run, names, snapshot, limit)

        with patch.object(journal_history, "read_events", delayed):
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(read_run, self.root, identifier)
                try:
                    self.assertTrue(reading.wait(5))
                    mutate()
                finally:
                    release.set()
                return pending.result(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
