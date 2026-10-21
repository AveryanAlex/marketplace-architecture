.PHONY: generate sync build up down logs check test test-local lint e2e sql

generate:
	./scripts/generate.sh

sync:
	uv sync --frozen

build:
	docker compose build api migrate

up:
	docker compose up --build -d --wait db migrate api

down:
	docker compose down

logs:
	docker compose logs -f api migrate

check:
	curl --fail --silent --show-error http://$$(docker compose port api 8080)/health

test:
	docker compose --profile test run --build --rm test

test-local: generate
	docker compose --profile test up -d --wait test-db
	DATABASE_URL=postgresql+asyncpg://marketplace:marketplace@localhost:$${TEST_DB_PORT:-15433}/marketplace_test PYTHONPATH=generated/src uv run alembic upgrade head
	DATABASE_URL=postgresql+asyncpg://marketplace:marketplace@localhost:$${TEST_DB_PORT:-15433}/marketplace_test uv run pytest -q

lint:
	uv run ruff check app tests scripts migrations
	uv run ruff format --check app tests scripts migrations

e2e:
	python3 scripts/e2e.py

sql:
	docker compose exec db psql -U marketplace -d marketplace

.PHONY: diagram
diagram:
	docker run --rm --user 0:0 -v "$(CURDIR)/docs:/work" plantuml/plantuml:1.2025.7 -tsvg /work/container.puml
	docker run --rm --user 0:0 -v "$(CURDIR)/docs:/work" plantuml/plantuml:1.2025.7 -tpng /work/container.puml
