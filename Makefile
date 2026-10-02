.PHONY: test up down check diagram

test:
	python3 -m unittest discover -v

up:
	docker compose up --build -d

down:
	docker compose down

check:
	curl --fail --silent --show-error http://localhost:8080/health

diagram:
	docker run --rm --user 0:0 \
		-v "$(CURDIR):/work" plantuml/plantuml \
		-tsvg -o /work/docs /work/docs/container.puml
	docker run --rm --user 0:0 \
		-v "$(CURDIR):/work" plantuml/plantuml \
		-tpng -o /work/docs /work/docs/container.puml
