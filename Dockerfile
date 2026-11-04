FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY proto ./proto
COPY scripts/generate_proto.py ./scripts/generate_proto.py
COPY app ./app
RUN python scripts/generate_proto.py
COPY migrations ./migrations
COPY scripts/flight_health.py ./scripts/flight_health.py
RUN useradd --system --uid 10001 booking && chown -R booking:booking /app
USER booking
EXPOSE 8080 50051
CMD ["uvicorn", "app.booking.main:app", "--host", "0.0.0.0", "--port", "8080"]

FROM base AS test
USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY pyproject.toml .
COPY tests ./tests
USER booking
CMD ["python", "-m", "pytest", "-v", "-p", "no:cacheprovider"]
