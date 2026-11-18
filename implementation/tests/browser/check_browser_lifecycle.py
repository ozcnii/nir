"""Stop and fault tests against a disposable PostgreSQL and local HTTP fixture."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.request import urlopen

import django
from django.db import connection, transaction
from psycopg import Connection
from psycopg.rows import TupleRow

from collection.browser import BrowserWork
from collection.runner import run_with_browser
from collection.selection import Direction, Selection, parse
from config.collection_lock import try_delete_lock
from config.control import CONTROL_PATH, request
from config.launcher import open_pidfd, send_pidfd, serve

START: dict[str, object] = {
    "action": "start",
    "selection": {
        "sources": ["cian"],
        "regions": [68],
        "categories": ["resale"],
        "deal_types": ["sale"],
    },
}


def until(predicate: Callable[[], bool], timeout: float = 12) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for lifecycle fixture")
        time.sleep(0.03)


def marker(path: Path, value: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(value)
    temporary.replace(path)


def children(pid: int) -> list[int]:
    path = Path(f"/proc/{pid}/task/{pid}/children")
    return [int(child) for child in path.read_text().split()]


def command_line(pid: int) -> list[str]:
    return Path(f"/proc/{pid}/cmdline").read_text().split("\x00")


def fixture_web(root: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/exit-web":
                os._exit(17)
            with (root / "requests").open("a") as output:
                print(self.path, file=output)
            if self.path in ("/slow", "/retry"):
                (root / "http-entered").touch()
                until((root / "http-release").exists)
            status = 500 if self.path == "/retry" else 200
            html = f"<title>{self.path}</title><p>{self.path}</p>".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        marker(root / "port", str(server.server_port))
        server.serve_forever()


class AbortWrite(Exception):
    pass


def fixture_runner(root: Path, raw_selection: str) -> int:
    base = f"http://127.0.0.1:{(root / 'port').read_text()}"
    mode = (root / "mode").read_text()

    def body(work: BrowserWork, _selection: Selection) -> None:
        worker = os.getpid()
        driver = next(
            pid for pid in children(worker) if "run-driver" in command_line(pid)
        )
        browser = next(
            pid
            for pid in children(driver)
            if command_line(pid)[0].startswith("/ms-playwright/")
        )
        marker(root / "worker", str(worker))
        marker(root / "driver", str(driver))
        marker(root / "chromium", str(browser))
        marker(root / "profile", os.environ["TMPDIR"])

        if mode == "smoke":
            listing, card = work.listing, work.card
            work.navigate("listing", base + "/listing")
            work.navigate("card", base + "/card/1")
            first = card.title()
            work.navigate("card", base + "/card/2")
            second = card.title()
            listing_title = listing.title()
            work.navigate("listing", "chrome://sandbox")
            report = {
                "identity": [os.getuid(), os.getgid()],
                "command": command_line(browser),
                "pages": len(card.context.pages),
                "same_pages": listing is work.listing and card is work.card,
                "titles": [listing_title, first, second],
                "sandbox": listing.locator("body").inner_text(),
            }
            marker(root / "smoke", json.dumps(report))
            return

        if mode in ("slow", "retry"):
            work.navigate("listing", base + "/" + mode)
            work.navigate("card", base + "/next")
            return

        if mode in ("before-write", "commit", "rollback"):
            item_id = {"before-write": 10, "commit": 20, "rollback": 30}[mode]

            def operation(conn: Connection[TupleRow]) -> None:
                conn.execute(
                    "INSERT INTO public.t10_probe (id) VALUES (%s)", (item_id,)
                )
                if mode != "before-write":
                    (root / "inside-write").touch()
                    until((root / "write-release").exists)
                    if mode == "rollback":
                        raise AbortWrite

            if mode == "before-write":
                (root / "about-to-write").touch()
                until((root / "write-release").exists)
            try:
                work.write(operation)
            except AbortWrite:
                pass
            work.navigate("card", base + "/next")
            return

        if mode == "hang":
            until((root / "browser-stopped").exists)
            (root / "hang-started").touch()
            work.navigate("listing", base + "/hanging")
            return

        while True:
            work.checkpoint()
            work.listing.title()
            time.sleep(0.05)

    result = run_with_browser(
        parse(json.loads(raw_selection)),
        supported=lambda direction: (
            direction == Direction("cian", 68, "resale", "sale")
        ),
        body=body,
    )
    if root.name == "app-term" and result == 0:
        print("T10: browser runner completed cooperative cleanup.", file=sys.stderr)
    return result


def fixture_launcher(root: Path, endpoint: Path) -> int:
    web = [sys.executable, __file__, "web", str(root)]
    worker = [sys.executable, __file__, "worker", str(root)]
    return serve(endpoint, web, worker)


class BrowserLifecycleTests(unittest.TestCase):
    def start_launcher(self, mode: str) -> tuple[Path, Path, subprocess.Popen[bytes]]:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        marker(root / "mode", mode)
        endpoint = root / "control.sock"
        launcher = subprocess.Popen([sys.executable, __file__, "launcher", str(root)])
        self.addCleanup(self.finish_launcher, launcher)
        until(endpoint.exists)
        until((root / "port").exists)
        return root, endpoint, launcher

    @staticmethod
    def finish_launcher(launcher: subprocess.Popen[bytes]) -> None:
        if launcher.poll() is None:
            launcher.terminate()
            launcher.wait(timeout=52)

    @staticmethod
    def status(endpoint: Path, expected: str, timeout: float = 12) -> dict[str, object]:
        value: dict[str, object] = {}

        def reached() -> bool:
            value.update(request({"action": "status"}, endpoint))
            return value.get("state") == expected

        until(reached, timeout)
        return value

    @staticmethod
    def pidfd(pid: int) -> int:
        return open_pidfd(pid)

    def owned_fd(self, root: Path, name: str) -> int:
        fd = self.pidfd(int((root / name).read_text()))
        self.addCleanup(os.close, fd)
        return fd

    @staticmethod
    def base(root: Path) -> str:
        return f"http://127.0.0.1:{(root / 'port').read_text()}"

    def observed_cleanup(self, root: Path, descriptors: tuple[int, ...]) -> None:
        for fd in descriptors:
            with self.assertRaises(ProcessLookupError):
                send_pidfd(fd, 0)
        self.assertFalse(Path((root / "profile").read_text()).exists())
        with transaction.atomic():
            self.assertTrue(try_delete_lock())

    def assert_fenced(self, endpoint: Path, status: dict[str, object]) -> None:
        from catalog.services import ReferenceDeleteBlocked, delete_references

        self.assertIs(status["cleanup_confirmed"], False)
        self.assertIs(request(START, endpoint)["ok"], False)
        with self.assertRaises(ReferenceDeleteBlocked):
            delete_references("regions", (68,), socket_path=endpoint)

    def test_bundled_chromium_sandbox_and_page_reuse(self) -> None:
        # Arrange
        root, endpoint, _ = self.start_launcher("smoke")
        idle = self.status(endpoint, "idle")
        launched_at_startup = (root / "worker").exists()

        # Act
        request(START, endpoint)
        result = self.status(endpoint, "finished")
        report = cast(dict[str, object], json.loads((root / "smoke").read_text()))
        chrome = cast(list[str], report["command"])
        sandbox = cast(str, report["sandbox"])

        # Assert
        self.assertIs(idle["cleanup_confirmed"], True)
        self.assertFalse(launched_at_startup)
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(report["identity"], [10001, 10001])
        self.assertIn("/ms-playwright/chromium-", chrome[0])
        self.assertTrue(
            any(arg == "--headless" or arg.startswith("--headless=") for arg in chrome)
        )
        self.assertNotIn("--no-sandbox", chrome)
        self.assertRegex(sandbox, r"Layer 1 Sandbox\s+Namespace")
        self.assertRegex(sandbox, r"PID namespaces\s+Yes")
        self.assertRegex(sandbox, r"Network namespaces\s+Yes")
        self.assertRegex(sandbox, r"Seccomp-BPF sandbox\s+Yes")
        self.assertIn("You are adequately sandboxed", sandbox)
        self.assertEqual(report["pages"], 2)
        self.assertIs(report["same_pages"], True)
        self.assertEqual(report["titles"], ["/listing", "/card/1", "/card/2"])
        self.observed_cleanup(root, ())

    def test_stop_during_navigation_and_before_retry(self) -> None:
        for mode in ("slow", "retry"):
            with self.subTest(mode=mode):
                # Arrange
                root, endpoint, _ = self.start_launcher(mode)
                run = request(START, endpoint)
                until((root / "http-entered").exists)
                owned = tuple(
                    self.owned_fd(root, role)
                    for role in ("worker", "driver", "chromium")
                )

                # Act
                stopping = request(
                    {"action": "stop", "run_id": run["run_id"]}, endpoint
                )
                (root / "http-release").touch()
                result = self.status(endpoint, "finished")
                with urlopen(self.base(root) + "/health", timeout=2) as response:
                    web_status = response.status

                # Assert
                self.assertEqual(stopping["state"], "stopping")
                self.assertEqual(result["reason"], "stopped")
                self.assertEqual(web_status, 200)
                self.assertEqual(
                    (root / "requests").read_text().splitlines().count("/" + mode), 1
                )
                self.assertNotIn("/next", (root / "requests").read_text())
                self.observed_cleanup(root, owned)

    def test_stop_before_write_or_finish_inflight_transaction(self) -> None:
        for mode, expected in (
            ("before-write", ()),
            ("commit", (20,)),
            ("rollback", (20,)),
        ):
            with self.subTest(mode=mode):
                # Arrange
                root, endpoint, _ = self.start_launcher(mode)
                run = request(START, endpoint)
                until(
                    (
                        root
                        / (
                            "about-to-write"
                            if mode == "before-write"
                            else "inside-write"
                        )
                    ).exists
                )

                # Act
                request({"action": "stop", "run_id": run["run_id"]}, endpoint)
                (root / "write-release").touch()
                result = self.status(endpoint, "finished")
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id FROM public.t10_probe ORDER BY id")
                    rows = tuple(row[0] for row in cursor.fetchall())

                # Assert
                self.assertEqual(result["reason"], "stopped")
                self.assertEqual(rows, expected)
                self.assertFalse((root / "requests").exists())
                self.observed_cleanup(root, ())

    def test_stale_stop_does_not_signal_new_runner(self) -> None:
        # Arrange
        root, endpoint, _ = self.start_launcher("idle")
        first = request(START, endpoint)
        self.status(endpoint, "running")
        until((root / "worker").exists)
        old = self.owned_fd(root, "worker")
        request({"action": "stop", "run_id": first["run_id"]}, endpoint)
        self.status(endpoint, "finished")
        (root / "worker").unlink()
        second = request(START, endpoint)
        self.status(endpoint, "running")
        until((root / "worker").exists)
        fresh = self.owned_fd(root, "worker")

        # Act
        answer = request({"action": "stop", "run_id": first["run_id"]}, endpoint)
        remaining = self.status(endpoint, "running")

        # Assert
        self.assertIs(answer["ok"], False)
        self.assertEqual(remaining["run_id"], second["run_id"])
        with self.assertRaises(ProcessLookupError):
            send_pidfd(old, 0)
        send_pidfd(fresh, 0)

    def test_runner_and_driver_death_leave_unknown_without_signalling_other_run(
        self,
    ) -> None:
        for target in ("worker", "driver"):
            with self.subTest(target=target):
                # Arrange
                # A separate Chrome in the test process is outside both runner subtrees.
                from playwright.sync_api import sync_playwright

                root, endpoint, _ = self.start_launcher("idle")
                request(START, endpoint)
                self.status(endpoint, "running")
                until((root / target).exists)
                victim = self.owned_fd(root, target)

                with sync_playwright() as playwright:
                    independent = playwright.chromium.launch(
                        channel="chromium", chromium_sandbox=True
                    )
                    page = independent.new_page()
                    page.set_content("<title>Independent</title>")

                    # Act
                    send_pidfd(victim, signal.SIGKILL)
                    outcome = self.status(endpoint, "unknown")
                    unaffected = page.title()

                    independent.close()

                # Assert
                self.assert_fenced(endpoint, outcome)
                self.assertEqual(unaffected, "Independent")

    def test_hung_chromium_forces_only_runner_after_stop_deadline(self) -> None:
        # Arrange
        root, endpoint, _ = self.start_launcher("hang")
        initial = request(START, endpoint)
        self.status(endpoint, "running")
        until((root / "chromium").exists)
        chromium = self.owned_fd(root, "chromium")
        worker = self.owned_fd(root, "worker")
        send_pidfd(chromium, signal.SIGSTOP)
        (root / "browser-stopped").touch()
        until((root / "hang-started").exists)

        try:
            # Act
            start = time.monotonic()
            response = request(
                {"action": "stop", "run_id": initial["run_id"]}, endpoint
            )
            outcome = self.status(endpoint, "unknown", timeout=47)
            elapsed = time.monotonic() - start

            # Assert
            self.assertEqual(response["state"], "stopping")
            self.assertGreaterEqual(elapsed, 39.5)
            self.assertLess(elapsed, 45)
            self.assertEqual(outcome["reason"], "forced_stop")
            self.assert_fenced(endpoint, outcome)
            until(lambda: not Path(f"/proc/{(root / 'worker').read_text()}").exists())
            with self.assertRaises(ProcessLookupError):
                send_pidfd(worker, 0)
            send_pidfd(chromium, 0)
        finally:
            send_pidfd(chromium, signal.SIGCONT)

    def test_web_exit_stops_and_waits_for_its_runner(self) -> None:
        # Arrange
        root, endpoint, launcher = self.start_launcher("idle")
        request(START, endpoint)
        self.status(endpoint, "running")
        until((root / "chromium").exists)
        owned = tuple(
            self.owned_fd(root, role) for role in ("worker", "driver", "chromium")
        )

        # Act
        from http.client import RemoteDisconnected

        with self.assertRaises(RemoteDisconnected):
            urlopen(self.base(root) + "/exit-web", timeout=3).close()
        code = launcher.wait(timeout=12)

        # Assert
        self.assertEqual(code, 1)
        self.observed_cleanup(root, owned)


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if sys.argv[1:] and sys.argv[1] in ("web", "worker", "launcher", "app-launcher"):
        role, base = sys.argv[1:3]
        root = Path(base)
        if role == "web":
            fixture_web(root)
        elif role == "worker":
            raise SystemExit(fixture_runner(root, sys.argv[3]))
        elif role == "launcher":
            raise SystemExit(fixture_launcher(root, root / "control.sock"))
        else:
            root.mkdir(mode=0o700, exist_ok=True)
            marker(root / "mode", "idle")
            raise SystemExit(fixture_launcher(root, CONTROL_PATH))
    django.setup()
    unittest.main(verbosity=2)
