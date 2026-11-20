"""Решает задачи, которые сбор публикует в каталоге брокера.

Сбор кладёт задачу в `<root>/pending/<id>.json` рядом с картинкой `<id>.png`.
Этот процесс забирает задачи, зовёт агента pi и пишет ответ в
`<root>/answers/<id>.json`.

Запускается там же, где лежит каталог — локальный путь, без внешних хостов.
Работает одинаково на macOS, Linux и Windows.

Требуется установленный и авторизованный `pi` (`pi auth`), модель должна уметь
смотреть изображения. Имя модели задаётся переменной `CAPTCHA_MODEL`, по
умолчанию `opencode-go/deepseek-v4.1-flash`.

    python implementation/tools/captcha_agent.py --root ./broker
    python implementation/tools/captcha_agent.py --root /var/lib/nir/broker --once
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

MODEL = os.environ.get("CAPTCHA_MODEL", "opencode-go/deepseek-v4.1-flash")
VIEWPORT = (1280, 720)

# Задача формулируется по типу картинки, а не по названию площадки: агенту
# показывают, что нужно сделать, и он решает это как обычную визуальную задачу.
TASKS = {
    "puzzle": (
        "На картинке головоломка: часть изображения вырезана и её нужно "
        "переместить на пустое место. Найди вырезанный фрагмент и пустое место, "
        "посчитай горизонтальный сдвиг."
    ),
    "unfold": (
        "На картинке изображение, искажённое по горизонтали. Нужно переместить "
        "бегунок так, чтобы пропорции восстановились."
    ),
    "button": (
        "На картинке есть элемент, который нужно нажать, чтобы продолжить. "
        "Найди его и верни координаты."
    ),
}

PROMPT = """Прочитай изображение {image} инструментом read и реши задачу.

{task}

Координаты — в пикселях экрана {width}x{height}. На снимке они совпадают с экранными.

Верни СТРОГО один JSON, без markdown и без пояснений:

{{"action": "drag", "start": [x, y], "dx": <целое 20..600>}}
    — если нужно тянуть бегунок: start — его центр, dx — сдвиг вправо.

{{"action": "click", "x": <x>, "y": <y>}}
    — если нужно нажать на элемент.

{{"action": "give_up", "reason": "..."}}
    — если по картинке решить нельзя.

Если нужны вычисления (расстояния, координаты) — используй python, а не считай в уме.
"""

# Признак в запросе -> тип задачи. Первое совпадение выигрывает.
MARKERS = (
    ("puzzle", ("geetest",)),
    ("unfold", ("unfold", "servicepipe", "xpvnsulc")),
    ("button", ("button", "notice", "block")),
)


def task_kind(marker: str) -> str:
    marker = marker.lower()
    for kind, tokens in MARKERS:
        if any(token in marker for token in tokens):
            return kind
    return "button"


def ask(image: Path, kind: str) -> dict[str, object] | None:
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
        timeout=180,
    )
    text = run.stdout
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def valid(answer: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(answer, dict):
        return None
    width, height = VIEWPORT
    try:
        if answer.get("action") == "give_up":
            return {"action": "give_up"}
        if answer.get("action") == "click":
            x, y = float(answer["x"]), float(answer["y"])
            if 0 <= x < width and 0 <= y < height:
                return {"action": "click", "x": round(x), "y": round(y)}
        if answer.get("action") == "drag":
            sx, sy = (float(value) for value in answer["start"])
            dx = float(answer["dx"])
            if 0 <= sx < width and 0 <= sy < height and 0 <= dx <= 600:
                return {
                    "action": "drag",
                    "start": [round(sx), round(sy)],
                    "dx": round(dx),
                }
    except (KeyError, TypeError, ValueError):
        return None
    return None


def main() -> int:
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
            answer = valid(ask(image, kind))
            if answer is None:
                print(
                    f"{request_path.stem}: агент не дал применимого ответа", flush=True
                )
                answer = {"action": "give_up"}
            answer_path.write_text(json.dumps(answer), encoding="utf-8")
            print(f"{request_path.stem}: {kind} -> {answer}", flush=True)
        if options.once:
            return 0
        time.sleep(options.poll)


if __name__ == "__main__":
    raise SystemExit(main())
