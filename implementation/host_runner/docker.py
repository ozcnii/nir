import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

PROJECT = "com.docker.compose.project"
SERVICE = "com.docker.compose.service"
ONEOFF = "com.docker.compose.oneoff"
GENERATION = "org.nir.runner.generation"
SERVICES = ("app", "db")


class DockerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Container:
    id: str
    labels: dict[str, str]
    running: bool
    url: str | None

    @property
    def service(self) -> str:
        return self.labels.get(SERVICE, "")


class Docker:
    def __init__(self, root: Path, project: str, lock_fd: int) -> None:
        self.root = root
        self.project = project
        self.lock_fd = lock_fd

    def command(self, *args: str, env: dict[str, str] | None = None) -> str:
        # The CLI keeps the same flock if the runner dies during a mutation.
        result = subprocess.run(
            ["docker", *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            pass_fds=(self.lock_fd,),
            start_new_session=True,
            check=False,
        )
        if result.returncode:
            raise DockerError(
                result.stderr.strip() or f"Docker returned {result.returncode}"
            )
        return result.stdout.strip()

    def daemon(self) -> str:
        identity = self.command("info", "--format", "{{.ID}}")
        if not identity or identity == "<no value>":
            raise DockerError("Cannot identify Docker Engine")
        return identity

    def containers(self) -> list[Container]:
        ids = self.command(
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label={PROJECT}={self.project}",
        ).splitlines()
        if not ids:
            return []
        records = cast(
            list[dict[str, object]],
            json.loads(self.command("container", "inspect", *ids)),
        )
        if {record["Id"] for record in records} != set(ids):
            raise DockerError("Docker inspect did not account for all containers")
        containers = []
        for record in records:
            config = cast(dict[str, dict[str, str]], record["Config"])
            labels = config["Labels"]
            if labels.get(ONEOFF, "").lower() == "true":
                continue
            state = cast(dict[str, bool], record["State"])
            network = cast(
                dict[str, dict[str, list[dict[str, str]]]], record["NetworkSettings"]
            )
            mappings = (network.get("Ports") or {}).get("8000/tcp") or []
            url = None
            if len(mappings) == 1 and mappings[0]["HostIp"] == "127.0.0.1":
                port = int(mappings[0]["HostPort"])
                if 0 < port <= 65535:
                    url = f"http://127.0.0.1:{port}"
            containers.append(
                Container(str(record["Id"]), labels, state["Running"], url)
            )
        return containers

    def start(self, generation: str, port: str, db_only: bool) -> None:
        self.command(
            "compose",
            "--file",
            "compose.yaml",
            "--project-name",
            self.project,
            "up",
            "--wait",
            "--no-recreate",
            *(("db",) if db_only else SERVICES),
            env={**os.environ, "NIR_RUNNER_GENERATION": generation, "APP_PORT": port},
        )

    @staticmethod
    def port_conflict(error: DockerError) -> bool:
        return (
            re.search(
                r"127\.0\.0\.1:8000\b[^\n]*(?:address already in use|port is already allocated)"
                r"|(?:address already in use|port is already allocated)[^\n]*127\.0\.0\.1:8000\b",
                str(error),
                re.IGNORECASE,
            )
            is not None
        )

    def stop(self, container_id: str) -> None:
        self.command("container", "stop", container_id)

    def remove(self, container_id: str) -> None:
        self.command("container", "rm", container_id)
