"""Запускает сбор вместе с решателем задач: одна команда, без ручных шагов.

Обёртка поднимает `tools/captcha_agent.py`, запускает `manage.py collect` с
каталогом брокера и гасит решателя по завершении сбора. Если задача встретится,
её решит агент; если нет — обёртка просто отработает как обычный сбор.

    python implementation/tools/collect_with_agent.py \
        -- --source cian --region 68 --property-type apartment --deal sale

Всё после `--` передаётся команде `collect` без изменений. Каталог брокера
задаётся через `--root` (по умолчанию `.broker` в текущем каталоге).

Условия: `pi` установлен и авторизован (`pi auth`), модель умеет смотреть
изображения; имя модели задаётся `CAPTCHA_MODEL`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

AGENT = Path(__file__).with_name("captcha_agent.py")


def pi_ready(model: str) -> bool:
    """Проверка до запуска: без готового pi и модели решать задачи некому."""
    if shutil.which("pi") is None:
        return False
    done = subprocess.run(
        ["pi", "auth", "check", "--model", model],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--root", type=Path, default=Path(".broker"))
    parser.add_argument("--wait", default="300", help="сколько ждать ответ на задачу")
    parser.add_argument("collect_args", nargs=argparse.REMAINDER)
    options = parser.parse_args()

    collect_args = [arg for arg in options.collect_args if arg != "--"]
    if not collect_args:
        parser.error("нужны аргументы для collect после --")

    model = os.environ.get("CAPTCHA_MODEL", "openai-codex/gpt-6-luna")
    if not pi_ready(model):
        print(
            f"pi не готов: нужен авторизованный pi и модель {model}, "
            "умеющая смотреть изображения",
            file=sys.stderr,
        )
        return 2

    root = options.root.resolve()
    (root / "pending").mkdir(parents=True, exist_ok=True)
    (root / "answers").mkdir(parents=True, exist_ok=True)

    agent = subprocess.Popen(
        [sys.executable, str(AGENT), "--root", str(root)],
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    print(f"решатель поднят, каталог задач: {root}", file=sys.stderr, flush=True)
    try:
        time.sleep(1.0)
        if agent.poll() is not None:
            print("решатель не запустился", file=sys.stderr)
            return 2
        collection = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.manage",
                "collect",
                *collect_args,
                "--captcha-broker",
                str(root),
                "--captcha-wait",
                options.wait,
            ],
            check=False,
        )
        return collection.returncode
    finally:
        agent.terminate()
        try:
            agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            agent.kill()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
