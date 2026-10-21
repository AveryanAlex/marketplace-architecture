FROM eclipse-temurin:21-jre-alpine AS codegen
RUN apk add --no-cache curl
WORKDIR /build
COPY scripts/generate.sh scripts/generate.sh
COPY codegen codegen
COPY openapi openapi
RUN ./scripts/generate.sh

FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.13-slim AS dependencies
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

FROM dependencies AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 \
    PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/generated/src" UV_CACHE_DIR=/tmp/uv-cache
RUN useradd --create-home marketplace
COPY --from=codegen /build/generated/src generated/src
COPY app app
COPY migrations migrations
COPY alembic.ini ./
COPY openapi openapi
USER marketplace
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"]
CMD ["python", "-m", "app.main"]

FROM runtime AS test
ENV RUFF_CACHE_DIR=/tmp/ruff-cache
USER root
RUN uv sync --frozen && mkdir -p .pytest_cache && chown marketplace .pytest_cache
COPY tests tests
COPY scripts/e2e.py scripts/e2e.py
USER marketplace
CMD ["sh", "-c", "alembic upgrade head && pytest -q"]

FROM runtime AS production
