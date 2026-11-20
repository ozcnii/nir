"""Решает задачи, которые сбор публикует в каталоге брокера.

Сбор кладёт задачу в `<root>/pending/<id>.json` рядом с картинкой `<id>.png`.
Этот процесс забирает задачи, зовёт агента pi и пишет ответ в
`<root>/answers/<id>.json`.

Запускается там же, где лежит каталог — локальный путь, без внешних хостов.
Работает одинаково на macOS, Linux и Windows.

Требуется установленный и авторизованный `pi` (`pi auth`), модель должна уметь
смотреть изображения. Имя модели обязательно задаётся переменной
`CAPTCHA_MODEL`; без неё запуск завершается с ошибкой.

    python implementation/tools/captcha_agent.py --root ./broker
    python implementation/tools/captcha_agent.py --root /var/lib/nir/broker --once
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MODEL = os.environ.get("CAPTCHA_MODEL", "").strip()
# Сколько ждать ответа модели на одну задачу. Ориентир — самая медленная
# пригодная модель: пазл Авито на deepseek занимает около 240 с. По истечении
# задача получает give_up.
ANSWER_TIMEOUT_S = float(os.environ.get("CAPTCHA_TIMEOUT", "360"))
VIEWPORT = (1280, 720)

# Задача формулируется как измерение по картинке. Так модель отвечает на
# вопрос о геометрии, а действие собирает код — и отказ не возникает там, где
# спрашивают про расстояние, а не про выполнение.
TASKS = {
    "puzzle": (
        "На картинке головоломка: часть изображения вырезана, для неё есть пустое "
        "место. Измерь горизонтальное расстояние между фрагментом и пустым местом."
    ),
    "unfold": (
        "На картинке изображение в круге искажено по горизонтали, ниже ползунок. "
        "Измерь, на сколько пикселей вправо сдвинуть бегунок, чтобы пропорции "
        "изображения восстановились."
    ),
    "button": (
        "На картинке есть элемент, нажатие на который продолжает работу. "
        "Определи его центр."
    ),
}

PROMPT = """Открой изображение {image} и выполни измерение.

{task}

Изображение — скриншот экрана {width}x{height}; координаты на нём совпадают с экранными.

Ответь одной строкой, без пояснений и без markdown:
    dx=<целое> center=(<x>, <y>)
где dx — сдвиг вправо в пикселях, center — центр круглого бегунка ползунка.
Если задача про нажатие:
    click=(<x>, <y>)
Если измерить по картинке нельзя:
    give_up: <что именно не видно>
"""

# Признак в запросе -> тип задачи. Первое совпадение выигрывает.
MARKERS = (
    ("puzzle", ("geetest",)),
    ("unfold", ("unfold", "servicepipe", "xpvnsulc")),
    ("button", ("button", "notice", "block")),
)

_DRAG = re.compile(r"dx\s*=\s*(-?\d+).*?\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.S)
_CLICK = re.compile(r"click\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.S)
_GIVE_UP = re.compile(r"give_up\s*:?\s*(.*)", re.S)


def task_kind(marker: str) -> str:
    marker = marker.lower()
    for kind, tokens in MARKERS:
        if any(token in marker for token in tokens):
            return kind
    return "button"


def ask(image: Path, kind: str) -> str:
    prompt = PROMPT.format(
        image=image.as_posix(),
        task=TASKS[kind],
        width=VIEWPORT[0],
        height=VIEWPORT[1],
    )
    run = subprocess.run(
        ["pi", "-p", "--model", MODEL, prompt],
        capture_output=True,
        text=True,
        timeout=ANSWER_TIMEOUT_S,
    )
    return run.stdout.strip()


def parse(answer: str) -> dict[str, object] | None:
    width, height = VIEWPORT
    if match := _DRAG.search(answer):
        dx, x, y = (int(value) for value in match.groups())
        if 0 <= x < width and 0 <= y < height and 0 <= dx <= 600:
            return {"action": "drag", "start": [x, y], "dx": dx}
    if match := _CLICK.search(answer):
        x, y = (int(value) for value in match.groups())
        if 0 <= x < width and 0 <= y < height:
            return {"action": "click", "x": x, "y": y}
    if _GIVE_UP.search(answer):
        return {"action": "give_up"}
    if (start := answer.find("{")) >= 0 and (end := answer.rfind("}")) > start:
        try:
            payload = json.loads(answer[start : end + 1])
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict) and payload.get("action") in {"drag", "click"}:
            return payload  # старый формат: проверяется ниже по общим правилам
    return None


def main() -> int:
    if not MODEL:
        print("нужна переменная CAPTCHA_MODEL с именем модели", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll", type=float, default=2.0)
    options = parser.parse_args()

    pending = options.root / "pending"
    answers = options.root / "answers"
    answers.mkdir(parents=True, exist_ok=True)
    print(f"каталог задач: {pending}", flush=True)
    while True:
        for request_path in sorted(pending.glob("*.json")):
            answer_path = answers / request_path.name
            if answer_path.exists():
                continue
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            image = pending / str(request.get("screenshot", ""))
            if not image.is_file():
                continue
            kind = task_kind(str(request.get("marker", "")))
            try:
                raw = ask(image, kind)
            except subprocess.TimeoutExpired:
                print(
                    f"{request_path.stem}: ответа нет за {ANSWER_TIMEOUT_S:.0f} с, "
                    "задача получает give_up",
                    flush=True,
                )
                raw = ""
            answer = parse(raw) or {"action": "give_up"}
            answer_path.write_text(json.dumps(answer), encoding="utf-8")
            print(
                f"{request_path.stem}: {kind} -> {answer} | ответ: {raw[:70]!r}",
                flush=True,
            )
        if options.once:
            return 0
        time.sleep(options.poll)


if __name__ == "__main__":
    raise SystemExit(main())
