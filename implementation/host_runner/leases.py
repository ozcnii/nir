import fcntl
import json
import os
import tempfile
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO, Literal, NotRequired, TypedDict, cast

from host_runner.docker import (
    GENERATION,
    PROJECT,
    SERVICES,
    Container,
    Docker,
    DockerError,
)
from host_runner.process import birth, group_exists

HEARTBEAT_INTERVAL = 5.0


class Lease(TypedDict):
    pid: int
    birth: str
    heartbeat: float
    task_group: NotRequired[int]


class State(TypedDict):
    project: str
    daemon: str
    generation: str | None
    phase: Literal["starting", "ready", "stopping"]
    containers: dict[str, dict[str, str]]
    leases: dict[str, Lease]


class LeaseError(RuntimeError):
    pass


class LeaseStore:
    def __init__(self, root: Path, project: str) -> None:
        self.root = root
        self.directory = root / ".local/runtime"
        self.path = self.directory / "leases.json"
        self.project = project

    @contextmanager
    def transaction(self) -> Iterator[Docker]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / "leases.lock").open("ab") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield Docker(self.root, self.project, lock.fileno())

    def hold_task(self, token: str) -> BinaryIO:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        guard = self.guard_path(token).open("xb")
        fcntl.flock(guard, fcntl.LOCK_EX)
        return guard

    def guard_path(self, token: str) -> Path:
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise LeaseError("Invalid lease token")
        return self.directory / f"task-{token}.lock"

    def load(self) -> State | None:
        try:
            return cast(State, json.loads(self.path.read_text()))
        except FileNotFoundError:
            return None

    def save(self, state: State) -> None:
        fd, path = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(state, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(path, self.path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(path)

    def forget(self, state: State, token: str) -> None:
        del state["leases"][token]
        self.save(state)
        self.guard_path(token).unlink()

    def recover_leases(self, state: State) -> None:
        for token, owner in list(state["leases"].items()):
            try:
                alive = birth(owner["pid"]) == owner["birth"]
                if alive:
                    continue
                if not self.task_finished(token, owner):
                    continue
            except OSError:
                # An unknown owner or a foreground task still holding its guard protects
                # the stack. A missing guard also requires operator investigation.
                continue
            self.forget(state, token)

    def owned(self, state: State, docker: Docker) -> list[Container]:
        containers = docker.containers()
        for container in containers:
            labels = container.labels
            if (
                container.service not in SERVICES
                or labels.get(PROJECT) != state["project"]
                or labels.get(GENERATION) != state["generation"]
                or (
                    state["phase"] != "starting"
                    and state["containers"].get(container.id) != labels
                )
            ):
                raise LeaseError(
                    "Container ownership changed; leaving the stack intact"
                )
        if len({container.service for container in containers}) != len(containers):
            raise LeaseError("Ambiguous stack ownership; leaving the stack intact")
        return containers

    def stop(self, state: State, docker: Docker) -> None:
        if state["generation"] is None:
            self.path.unlink()
            return

        containers = self.owned(state, docker)
        state["containers"] = {item.id: item.labels for item in containers}
        state["phase"] = "stopping"
        self.save(state)
        for item in sorted(containers, key=lambda container: container.service):
            if item.running:
                docker.stop(item.id)
            present = {
                container.id: container for container in self.owned(state, docker)
            }
            if item.id in present and present[item.id].running:
                raise LeaseError(f"Docker did not confirm stopping {item.service}")
        for item in self.owned(state, docker):
            docker.remove(item.id)
        if self.owned(state, docker):
            raise LeaseError("Docker did not confirm removal of stopped containers")
        self.path.unlink()

    @staticmethod
    def ready_url(containers: Sequence[Container], db_only: bool) -> str | None:
        by_service = {container.service: container for container in containers}
        if (
            len(by_service) != len(containers)
            or set(by_service) not in ({"db"}, {"app", "db"})
            or not all(item.running for item in containers)
        ):
            raise LeaseError("Project containers are not a running app/db or db stack")
        if not by_service["db"].healthy:
            raise LeaseError("PostgreSQL is not healthy")
        if db_only:
            return None
        app = by_service.get("app")
        if app is None or app.url is None:
            raise LeaseError("No running app with a single loopback port")
        if not app.healthy:
            raise LeaseError("App startup healthcheck has not passed")
        return app.url

    def acquire(
        self,
        token: str,
        owner_birth: str,
        db_only: bool,
    ) -> tuple[str | None, str | None]:
        with self.transaction() as docker:
            daemon = docker.daemon()
            state = self.load()
            if state is not None:
                if (state["project"], state["daemon"]) != (self.project, daemon):
                    raise LeaseError(
                        "Recorded leases belong to a different Docker Engine"
                    )
                self.recover_leases(state)
                if not state["leases"]:
                    self.stop(state, docker)
                    state = None

            if state is None:
                return self.start_or_share(docker, daemon, token, owner_birth, db_only)

            containers = (
                self.owned(state, docker)
                if state["generation"] is not None
                else docker.containers()
            )
            if (
                state["phase"] != "ready"
                or {item.id: item.labels for item in containers} != state["containers"]
            ):
                raise LeaseError("The previously leased stack cannot be verified")
            url = self.ready_url(containers, db_only)
            state["leases"][token] = self.owner(owner_birth)
            self.save(state)
            return url, state["generation"]

    @staticmethod
    def owner(owner_birth: str) -> Lease:
        return {"pid": os.getpid(), "birth": owner_birth, "heartbeat": time.time()}

    def start_or_share(
        self,
        docker: Docker,
        daemon: str,
        token: str,
        owner_birth: str,
        db_only: bool,
    ) -> tuple[str | None, str | None]:
        generation = uuid.uuid4().hex
        containers = docker.containers()
        state: State = {
            "project": self.project,
            "daemon": daemon,
            "generation": None if containers else generation,
            "phase": "starting",
            "containers": {},
            "leases": {},
        }
        managed = not containers
        if managed:
            self.save(state)
        try:
            if managed:
                containers = self.start(docker, state, generation, db_only)
            elif any(item.labels.get(PROJECT) != self.project for item in containers):
                raise LeaseError("Unverified project labels")
            url = self.ready_url(containers, db_only)
            state["phase"] = "ready"
            state["containers"] = {item.id: item.labels for item in containers}
            state["leases"][token] = self.owner(owner_birth)
            self.save(state)
        except (DockerError, LeaseError, OSError):
            # Read the last committed transaction. A failed stopping transaction
            # is already recoverable and must not get a second destructive attempt.
            recorded = self.load() if managed else None
            if recorded is not None and recorded["phase"] == "starting":
                self.stop(recorded, docker)
            raise
        return url, state["generation"]

    def start(
        self, docker: Docker, state: State, generation: str, db_only: bool
    ) -> list[Container]:
        explicit = os.environ.get("APP_PORT")
        try:
            try:
                docker.start(generation, explicit or "8000", db_only)
            except DockerError as error:
                if db_only or explicit or not docker.port_conflict(error):
                    raise
                self.stop(state, docker)
                state["phase"] = "starting"
                state["containers"] = {}
                self.save(state)
                docker.start(generation, "0", db_only)
        except DockerError as error:
            if not db_only:
                with suppress(DockerError, LeaseError):
                    for container in self.owned(state, docker):
                        if container.service == "app":
                            reason = docker.preflight_failure(container.id)
                            if reason is not None:
                                error.add_note(reason)
            raise
        return self.owned(state, docker)

    def register_group(self, token: str, group: int) -> None:
        with self.transaction():
            state = self.load()
            if state is None:
                raise LeaseError("The active lease registry disappeared")
            state["leases"][token]["task_group"] = group
            self.save(state)

    def task_finished(self, token: str, owner: Lease) -> bool:
        group = owner.get("task_group")
        if group is not None and group_exists(group):
            return False
        with self.guard_path(token).open("rb") as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        return True

    def heartbeat(self, token: str) -> None:
        with self.transaction():
            state = self.load()
            if state is None:
                raise LeaseError("The active lease registry disappeared")
            state["leases"][token]["heartbeat"] = time.time()
            self.save(state)

    def release(self, token: str, *, wait_for_task: bool) -> None:
        with self.transaction() as docker:
            state = self.load()
            guard_path = self.guard_path(token)
            if state is None or token not in state["leases"]:
                with suppress(FileNotFoundError):
                    guard_path.unlink()
                return
            deadline = time.monotonic() + (5 if wait_for_task else 0)
            while not self.task_finished(token, state["leases"][token]):
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.02)
            self.forget(state, token)
            self.recover_leases(state)
            if not state["leases"]:
                if state["daemon"] != docker.daemon():
                    raise LeaseError(
                        "Cannot release containers on a different Docker Engine"
                    )
                self.stop(state, docker)
