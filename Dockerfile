FROM ghcr.io/astral-sh/uv:0.9.11 AS uv
FROM python:3.13-slim AS base
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app app
COPY schemas schemas
COPY migrations migrations
RUN useradd --uid 10001 --create-home cinema
USER cinema
CMD ["uvicorn", "app.producer.main:app", "--host", "0.0.0.0", "--port", "8000"]
FROM base AS test
USER root
RUN uv sync --frozen
COPY tests tests
COPY deploy/grafana/dashboards deploy/grafana/dashboards
USER cinema
ENV RUFF_CACHE_DIR=/tmp/ruff-cache
CMD ["pytest", "-p", "no:cacheprovider", "-v"]
FROM base AS runtime
