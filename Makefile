PYTHON ?= .venv/bin/python

.PHONY: install proto test integration lint up down check
install:
	uv venv --python 3.13 .venv
	uv pip sync --python $(PYTHON) requirements-dev.txt
proto:
	$(PYTHON) scripts/generate_proto.py
test:
	$(PYTHON) -m pytest -v -m 'not integration'
integration:
	docker compose --profile test run --build --rm tests
lint:
	.venv/bin/ruff check .
up:
	docker compose up --build -d --wait
down:
	docker compose down
check:
	curl --fail --silent --show-error http://localhost:$${HOST_PORT:-8080}/ready
