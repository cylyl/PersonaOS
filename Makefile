.PHONY: help up down logs psql migrate migrate-down seed test test-unit test-integration test-e2e lint format typecheck clean

PYTHON ?= python3
UV    ?= uv

help:
	@echo "PersonaOS v0.1 — common commands"
	@echo ""
	@echo "  make up               Start Postgres in the background"
	@echo "  make down             Stop and remove Postgres"
	@echo "  make logs             Tail Postgres logs"
	@echo "  make psql             Open psql against the dev DB"
	@echo "  make migrate          Run Alembic migrations"
	@echo "  make migrate-down     Rollback the last migration"
	@echo "  make seed             Load example workers from examples/"
	@echo "  make test             Run all tests"
	@echo "  make test-unit        Run unit tests only"
	@echo "  make test-integration Run integration tests only"
	@echo "  make test-e2e         Run end-to-end tests only"
	@echo "  make lint             Run ruff"
	@echo "  make format           Auto-format with ruff"
	@echo "  make typecheck        Run mypy"
	@echo "  make clean            Remove caches and __pycache__"

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f postgres

psql:
	docker compose exec postgres psql -U personaos -d personaos

migrate:
	$(UV) run alembic upgrade head

migrate-down:
	$(UV) run alembic downgrade -1

seed:
	$(UV) run python scripts/dev/seed.py

test:
	$(UV) run pytest

test-unit:
	$(UV) run pytest tests/unit

test-integration:
	$(UV) run pytest tests/integration

test-e2e:
	$(UV) run pytest tests/e2e

lint:
	$(UV) run ruff check .

format:
	$(UV) run ruff format .

typecheck:
	$(UV) run mypy src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
