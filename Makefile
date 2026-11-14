SHELL := /bin/bash
COMPOSE := docker compose
# Pipeline commands run in the worker image (it has Railpack, the Docker CLI, Graphify, ripgrep)
# with the working tree mounted, so they always use the current code and fixtures.
RUN_WORKER := $(COMPOSE) run --rm -T -v "$(CURDIR)":/app -w /app
RUNNER_IMAGE ?= hackproof-runner:1

.DEFAULT_GOAL := help
.PHONY: help up down logs migrate test test-integration lint format fixtures fixture-remotes seed-demo runner-image prune env-check

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "} {printf "  %-18s %s\n", $$1, $$2}'

env-check:
	@test -f .env || { echo "No .env file. Copy .env.example to .env and fill in the required values."; exit 1; }

up: env-check ## Build and start postgres, buildkit, api, worker and web
	$(COMPOSE) up -d --build

down: ## Stop everything (data volumes are kept)
	$(COMPOSE) down

logs: ## Follow service logs
	$(COMPOSE) logs -f --tail=100

migrate: env-check ## Apply database migrations
	$(COMPOSE) run --rm -T api alembic upgrade head

test: env-check ## Unit tests on the host (starts postgres for the database tests)
	$(COMPOSE) up -d --wait postgres
	uv run pytest

test-integration: env-check runner-image ## Docker-backed tests, run inside the worker image
	$(COMPOSE) up -d --wait postgres buildkit
	$(RUN_WORKER) -e TEST_DATABASE_URL=postgresql+asyncpg://hp:hp@postgres:5432/hp_test worker pytest -m integration

lint: ## Ruff (lint and format check) and mypy --strict on core/
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format: ## Apply ruff fixes and formatting
	uv run ruff check --fix .
	uv run ruff format .

# At M3 the suite only lists itself; `runner-image` becomes a prerequisite again when M5 adds the
# runner's files and the suite starts running tests (D-041).
fixtures: env-check ## Run the whole fixture suite and compare with expected results
	$(COMPOSE) up -d --wait postgres buildkit
	$(RUN_WORKER) worker python -m scripts.run_fixtures $(FIXTURE_ARGS)

fixture-remotes: ## Build the fixture repositories into fixtures/remotes (on the host: the worker mounts them read-only)
	uv run python -m scripts.make_history_fixtures

seed-demo: env-check runner-image ## Create the demo event and pre-run every job
	$(COMPOSE) up -d --wait postgres buildkit
	$(RUN_WORKER) worker python -m scripts.seed_demo

runner-image: ## Build the test runner image
	docker build -f runner/Dockerfile -t $(RUNNER_IMAGE) .

prune: ## Remove HackProof run containers, networks and built team images
	@ids=$$(docker ps -aq --filter label=hackproof.attempt); [ -z "$$ids" ] || docker rm -f $$ids
	@nets=$$(docker network ls -q --filter label=hackproof.attempt); [ -z "$$nets" ] || docker network rm $$nets
	@imgs=$$(docker images --filter reference='hp/*' -q | sort -u); [ -z "$$imgs" ] || docker rmi -f $$imgs
	@echo "pruned"
