"""Check that the runtime database is ready before starting the container."""

import argparse
import os
import subprocess
import sys

import django
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.db import DatabaseError
from django.db.migrations.exceptions import InconsistentMigrationHistory


def preflight() -> bool:
    try:
        django.setup()
        call_command("migrate", "--check", "--noinput", verbosity=0, skip_checks=False)
    except SystemExit as error:
        if error.code != 1:
            raise
        print("Runtime preflight failed: unapplied Django migrations.", file=sys.stderr)
        return False
    except (
        CommandError,
        DatabaseError,
        ImproperlyConfigured,
        InconsistentMigrationHistory,
    ) as error:
        print(
            f"Runtime preflight failed: migration check failed: {error}",
            file=sys.stderr,
        )
        return False

    acl = subprocess.run(
        [sys.executable, "-m", "runtime.db_roles", "check"],
        capture_output=True,
        text=True,
        check=False,
    )
    if acl.returncode:
        print(f"Runtime preflight failed: {acl.stderr.strip()}", file=sys.stderr)
        return False
    print("Runtime preflight: database is ready.")
    return True


def web_command() -> list[str]:
    return [
        sys.executable,
        "manage.py",
        "runserver",
        "0.0.0.0:8000",
        "--noreload",
        "--insecure",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    parser.parse_args()
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    os.environ.setdefault("PGCONNECT_TIMEOUT", "5")

    return 0 if preflight() else 1


if __name__ == "__main__":
    raise SystemExit(main())
