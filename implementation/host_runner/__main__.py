"""Command-line entry point for the local Docker lease runner."""

import argparse
import sys
from pathlib import Path

from host_runner.docker import DockerError
from host_runner.leases import LeaseError
from host_runner.task import run


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
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
