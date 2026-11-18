"""Bounded local JSON messages shared by launcher and its callers."""

import json
import socket
import time
from pathlib import Path
from typing import cast

CONTROL_PATH = Path("/run/app/control.sock")
MAX_MESSAGE = 4096
IO_TIMEOUT = 0.25


class ControlError(RuntimeError):
    pass


def decode(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise ControlError("invalid JSON") from error
    if not isinstance(value, dict):
        raise ControlError("expected a JSON object")
    return cast(dict[str, object], value)


def encode(value: dict[str, object]) -> bytes:
    raw = json.dumps(value, separators=(",", ":")).encode()
    if len(raw) >= MAX_MESSAGE:
        raise ControlError("message too long")
    return raw + b"\n"


def receive(sock: socket.socket, deadline: float) -> dict[str, object]:
    data = bytearray()
    while len(data) < MAX_MESSAGE:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("control deadline expired")
        sock.settimeout(remaining)
        chunk = sock.recv(MAX_MESSAGE - len(data))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in data:
            if data[-1:] != b"\n" or data.count(b"\n") != 1:
                raise ControlError("expected one control message")
            return decode(bytes(data[:-1]))
    raise ControlError("incomplete or oversized control message")


def request(value: dict[str, object], path: Path = CONTROL_PATH) -> dict[str, object]:
    deadline = time.monotonic() + IO_TIMEOUT
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(IO_TIMEOUT)
            connection.connect(str(path))
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.sendall(encode(value))
            return receive(connection, deadline)
    except OSError as error:
        raise ControlError("launcher status unavailable") from error
