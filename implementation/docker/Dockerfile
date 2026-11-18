FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

ENV UV_PROJECT_ENVIRONMENT=/opt/app-venv \
    UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/opt/app-venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY .python-version pyproject.toml uv.lock ./
# The bundled Playwright Node driver needs libatomic on Linux arm64 to run install.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && /usr/local/bin/python -m pip install --no-cache-dir uv==0.13.9 \
    && uv sync --locked --no-dev --python /usr/local/bin/python \
    && playwright install --with-deps --no-shell chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /root/.cache/uv /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app app \
    && install -d -m 0700 -o app -g app /var/lib/app

COPY manage.py ./
COPY config/ config/
COPY catalog/ catalog/
COPY collection/ collection/

ENV HOME=/home/app
USER 10001:10001

CMD ["python", "-m", "config.launcher"]
