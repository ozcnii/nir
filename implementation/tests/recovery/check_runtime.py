"""Controlled predecessor and probes for the real Docker recovery test."""

import hashlib
import json
import os
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path

from collection import runner
from collection.selection import Direction, Selection, parse
from runtime.control import CONTROL_PATH, ControlError, request
from runtime.journal import JOURNAL_ROOT
from runtime.journal_history import read_run
from runtime.launcher import serve
from runtime.postgres_locks import RunSession
from runtime.volume_owner import claim_volume

START: dict[str, object] = {
    "action": "start",
    "selection": {
        "sources": ["cian"],
        "regions": [68],
        "categories": ["resale"],
        "deal_types": ["sale"],
    },
}
CORRUPT_RUN = "f" * 32


def until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 8
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Recovery runtime fixture did not respond")
        time.sleep(0.03)


def observed(state: str) -> dict[str, object]:
    answer: dict[str, object] = {}

    def reached() -> bool:
        answer.update(request({"action": "status"}))
        return answer.get("state") == state

    until(reached)
    return answer


def fixture_run(raw: str) -> int:
    def fixed_body(
        _session: RunSession,
        _selection: Selection,
        stopped: Callable[[], bool],
        initialized: Callable[[], None],
    ) -> None:
        initialized()
        one = CONTROL_PATH.parent / "complete-next"
        if one.exists():
            one.unlink()
            return
        # This controlled predecessor stays in its own old process namespace.
        while not stopped():
            time.sleep(0.04)

    return runner.run(
        parse(json.loads(raw)),
        supported=lambda value: value == Direction("cian", 68, "resale", "sale"),
        body=fixed_body,
    )


def old_app() -> int:
    JOURNAL_ROOT.mkdir(exist_ok=True)
    damaged = JOURNAL_ROOT / CORRUPT_RUN
    damaged.mkdir()
    (damaged / "state.json").write_text("fixture: do not repair")
    (damaged / "events.jsonl").write_text("fixture: do not remove")
    return serve(
        CONTROL_PATH,
        [sys.executable, "-c", "import time; time.sleep(300)"],
        [sys.executable, __file__, "worker"],
    )


def identify_predecessor() -> None:
    # Arrange
    until(CONTROL_PATH.exists)

    # Act
    (CONTROL_PATH.parent / "complete-next").touch()
    good_id = str(request(START)["run_id"])
    observed("finished")
    good = (JOURNAL_ROOT / good_id / "state.json").read_bytes()
    request(START)
    running = observed("running")
    identifier = str(running["run_id"])
    path = JOURNAL_ROOT / identifier / "state.json"
    until(lambda: json.loads(path.read_text())["status"] == "running")

    # Assert
    assert stat.S_ISSOCK(CONTROL_PATH.stat().st_mode)
    assert not running["cleanup_confirmed"]
    print(
        f"{identifier} {running['instance_id']} {good_id} {hashlib.sha256(good).hexdigest()}"
    )


def assert_lifetime_lock() -> None:
    # Arrange
    children = Path("/proc/1/task/1/children").read_text().split()
    launcher = int(children[0])
    web = int(Path(f"/proc/{launcher}/task/{launcher}/children").read_text().split()[0])

    # Act
    web_fds = [
        descriptor.readlink() for descriptor in Path(f"/proc/{web}/fd").iterdir()
    ]
    try:
        fd = claim_volume(JOURNAL_ROOT.parent)
    except ControlError as error:
        reason = str(error)
    else:
        os.close(fd)
        raise AssertionError("A second launcher acquired the live app_data lock")

    # Assert
    assert all(descriptor.name != ".owner.lock" for descriptor in web_fds)
    assert reason == "application volume already has an owner"


def inspect_recovered(
    identifier: str, old_instance: str, good_id: str, good_digest: str
) -> None:
    # Arrange
    initial = observed("idle")
    initial_dirs = {path.name for path in JOURNAL_ROOT.iterdir()}
    old = read_run(JOURNAL_ROOT, identifier)
    damaged = read_run(JOURNAL_ROOT, CORRUPT_RUN)
    good = read_run(JOURNAL_ROOT, good_id)
    good_bytes = (JOURNAL_ROOT / good_id / "state.json").read_bytes()
    assert_lifetime_lock()

    # Act
    start = request(START)
    final = observed("finished")
    fresh = read_run(JOURNAL_ROOT, str(start["run_id"]))

    # Assert
    assert initial["instance_id"] != old_instance and initial["run_id"] is None
    assert stat.S_IMODE(CONTROL_PATH.stat().st_mode) == 0o600
    assert initial_dirs == {identifier, CORRUPT_RUN, good_id}
    assert good.snapshot is not None and good.snapshot["outcome"] == "completed"
    assert hashlib.sha256(good_bytes).hexdigest() == good_digest
    assert old.snapshot is not None
    assert old.snapshot["status"] == "finished" and old.snapshot["outcome"] == "error"
    assert old.snapshot["reason"] == "app_restarted"
    assert old.snapshot["instance_id"] == old_instance
    assert old.snapshot["cleanup_confirmed"] is True
    assert old.snapshot["progress"] == {"committed_writes": 0, "incomplete": True}
    assert [entry["event"] for entry in old.events] == ["armed", "ready"]
    assert damaged.kind == "snapshot_corrupt"
    assert (
        JOURNAL_ROOT / CORRUPT_RUN / "state.json"
    ).read_text() == "fixture: do not repair"
    assert fresh.snapshot is not None and fresh.snapshot["run_id"] != identifier
    assert final["reason"] == "unsupported_direction"
    assert fresh.snapshot["progress"] == {"committed_writes": 0}
    print(
        "T12: fresh instance/socket, interrupted state preserved as error; manual run used a new ID."
    )


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    mode = sys.argv[1]
    if mode == "worker":
        raise SystemExit(fixture_run(sys.argv[2]))
    if mode == "old-app":
        raise SystemExit(old_app())
    if mode == "identify":
        identify_predecessor()
    elif mode == "check":
        inspect_recovered(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
