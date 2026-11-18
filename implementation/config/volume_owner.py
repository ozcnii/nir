"""Exclusive container-side ownership following the host's Docker check."""

import fcntl
import os
import stat
from pathlib import Path

from config.control import ControlError


def valid_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def claim_volume(root: Path) -> int:
    generation = os.environ.get("NIR_RUNNER_GENERATION")
    proof = os.environ.pop("NIR_HOST_RECOVERY_GENERATION", None)
    if not valid_identity(generation) or proof != generation:
        raise ControlError("host has not confirmed the previous application stopped")

    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(directory).st_uid != os.geteuid():
            raise ControlError("application volume is not owned by the runtime user")
        fd = os.open(
            ".owner.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise ControlError("invalid application ownership lock")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise ControlError("application ownership lock is not private")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ControlError("application volume already has an owner") from error
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(directory)
