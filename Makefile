.PHONY: up down logs test lint loadtest install

install:
	pip install -r requirements-dev.txt

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api

# Needs the compose Postgres running (make up) because tests exercise real row locks.
test:
	pytest -v

lint:
	ruff check .

loadtest:
	python scripts/loadtest.py --stock 100 --buyers 500 --concurrency 100
