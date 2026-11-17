# База app: первичные доказательства

18 ноября 2026. Исследование для неутверждённого контейнерного профиля. Прочитаны опубликованные Docker Hub metadata и документация Python/Playwright. Образы не скачивались, сборка/установка/контейнеры не запускались.

## Предлагаемый базовый образ

Официальный [python image](https://hub.docker.com/_/python) перечисляет точный tag python:3.13.18-slim-bookworm. Это Docker Official Image на Debian Bookworm, а не официальный бинарный установщик Python.org. Источник фактического manifest: [Docker Hub API tag](https://hub.docker.com/v2/repositories/library/python/tags/3.13.18-slim-bookworm).

Проверенная metadata:

| Артефакт | Digest |
|---|---|
| Multi-platform manifest/index python:3.13.18-slim-bookworm | sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 |
| Linux amd64 manifest | sha256:f3dac913ab53b78c64f26d5518fcd7043d5a1a46522d6a493b598c3ed12f62a6 |
| Linux arm64 manifest | sha256:aed314f1f0c7e988b7aee5d889414ec977430bad37770966c52aff5366a13a06 |

Metadata last_updated: 2026-11-05. Индекс позволяет одной записью зафиксировать набор платформ; он не означает, что бинарные слои arm64/amd64 одинаковы. Выбор native architecture делает Docker; принудительный amd64 на Mac ARM64 не предлагается. Название tag может обновляться, поэтому для будущей сборки предлагается tag плюс закреплённый index digest.

Для сравнения read-only проверен python:3.13.18-bookworm: [API](https://hub.docker.com/v2/repositories/library/python/tags/3.13.18-bookworm), index sha256:cc55e4ded9c95f6ce2133b3d096db43f214d280c2a8af65d83e246c8fa7d168c. Slim имеет меньший исходный набор системных пакетов; это не обещание малого итогового image после установки Chromium и библиотек.

## Применимость Playwright

[System requirements](https://playwright.dev/python/docs/intro#system-requirements) перечисляют Debian12/13, Ubuntu22.04/24.04/26.04 на x86-64/arm64. Bookworm — Debian12. Наличие соответствующей платформы в требованиях не равно испытанной сборке нашего app.

[Документация Docker](https://playwright.dev/python/docs/docker) описывает построение собственного образа с установкой пакета Playwright, браузера и системных зависимостей. Для проекта используется уже принятый PlaywrightPython1.65.2 и соответствующий ему Chromium, а не версия из примера документации. В image надо установить эти зависимости отдельно: Python base их не содержит. Повторное использование браузера/host .venv с Mac не предлагается.

Официальные рекомендации отдельно рассматривают пользователя процесса и sandbox при crawling, init для дочерних процессов и память Chromium. Они не доказываются выбором base image. Конкретные runtime user/seccomp/init/shm/остановка остаются детализацией запуска 9.4; автоматически отключать sandbox или добавлять privileged/SYS_ADMIN из примера не предлагается.

## Связь с 8.5 и границы

Python3.13.18 уже принят. Новый base image не меняет Django5.2.19/uv0.13.9/Playwright1.65.2/psycopg3.3.9. uv и Python-зависимости добавляются по принятому lock-процессу; lock пока не создавался. Установленные apt-пакеты и итоговый image будут иметь собственные фактические версии/digest после разрешённой сборки; base digest и uv.lock не фиксируют автоматически все последующие загрузки.

Проверено существование опубликованных tags/digests/платформ и документированная применимость. Не проверены загрузка образа, python --version внутри него, успешная установка всех транзитивных зависимостей, Chromium/sandbox или проход app/db. Эти проверки нужны при реализации, не выдаются за выполненные сейчас.
