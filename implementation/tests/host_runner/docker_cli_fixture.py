"""Process-level fixture for just the Docker CLI calls used by the host runner."""

import json
import os
import sys
import time
from pathlib import Path
from typing import cast


def main() -> int:
    directory = Path(os.environ["FAKE_DOCKER"])
    registry = directory / "containers.json"
    args = sys.argv[1:]
    with (directory / "events").open("a") as output:
        print(" ".join(args), file=output)
    containers = cast(dict[str, dict[str, object]], json.loads(registry.read_text()))

    match args:
        case ["info", "--format", "{{.ID}}"]:
            print("fixture-engine")
        case ["container", "ls", *_options, selector] if selector.startswith("volume="):
            if (directory / "volume-probe-failed").exists():
                return 1
            for key, container in containers.items():
                if container.get("volume") == selector.removeprefix("volume="):
                    print(key)
        case ["container", "ls", *_options, selector]:
            project = selector.split("=")[2]
            for key, container in containers.items():
                config = cast(dict[str, dict[str, str]], container["Config"])
                if config["Labels"]["com.docker.compose.project"] == project:
                    print(key)
        case ["container", "inspect", *ids]:
            print(json.dumps([containers[key] for key in ids]))
        case [
            "compose",
            "--file",
            "compose.yaml",
            "--project-name",
            project,
            "up",
            "--wait",
            "--no-recreate",
            *services,
        ]:
            generation = os.environ["NIR_RUNNER_GENERATION"]
            port = os.environ["APP_PORT"]
            proof = os.environ.get("NIR_HOST_RECOVERY_GENERATION")
            assert proof == (generation if "app" in services else "")
            for name in services:
                cid = f"{generation}-{name}"
                containers[cid] = {
                    "Id": cid,
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": project,
                            "com.docker.compose.service": name,
                            "org.nir.runner.generation": generation,
                        }
                    },
                    "State": {"Running": True, "Health": {"Status": "healthy"}},
                    "NetworkSettings": {
                        "Ports": {
                            "8000/tcp": [
                                {
                                    "HostIp": "127.0.0.1",
                                    "HostPort": port,
                                }
                            ],
                        }
                        if name == "app"
                        else {}
                    },
                }
            registry.write_text(json.dumps(containers))
        case ["container", "stop", cid]:
            if (directory / "fail-stop").exists():
                return 1
            if (directory / "pause-stop").exists():
                while not (directory / "resume-stop").exists():
                    time.sleep(0.01)
            containers[cid]["State"] = {"Running": False}
            registry.write_text(json.dumps(containers))
        case ["container", "rm", cid]:
            state = cast(dict[str, bool], containers[cid]["State"])
            assert not state["Running"]
            del containers[cid]
            registry.write_text(json.dumps(containers))
        case _:
            print(f"Unexpected fixture command {args}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
