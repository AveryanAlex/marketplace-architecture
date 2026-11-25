.PHONY: up down logs test unit lint faults demo sql
up:
	docker compose up -d --build --wait --wait-timeout 300
down:
	docker compose down
logs:
	docker compose logs -f --tail=100 movie-service aggregation-service bootstrap
# Builds and starts every dependency, runs real Kafka/ClickHouse/PostgreSQL/S3/Grafana tests.
test:
	docker compose --profile test run --build --rm tests
unit:
	uv sync --frozen
	uv run pytest -m 'not integration'
lint:
	uv run ruff check .
	uv run ruff format --check .
faults: up
	python3 scripts/fault_checks.py
demo:
	python3 scripts/demo.py
sql:
	docker compose exec postgres psql -U cinema -d cinema -c 'SELECT * FROM daily_metrics ORDER BY metric_date DESC,metric_name,dimension LIMIT 40'
