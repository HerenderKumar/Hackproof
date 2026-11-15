SHELL := /bin/bash
COMPOSE := docker compose
# Pipeline commands run in the worker image (it has Railpack, the Docker CLI, Graphify, ripgrep)
# with the working tree mounted, so they always use the current code and fixtures.
RUN_WORKER := $(COMPOSE) run --rm -T -v "$(CURDIR)":/app -w /app
# The worker service mounts fixtures/llm read-only (D-051 point 23), and that mount wins over the
# working tree's, so the suites that record (drafts, audits) read and write it at another path.
RECORDINGS := -v "$(CURDIR)/fixtures/llm":/recordings -e LLM_REPLAY_DIR=/recordings
RUNNER_IMAGE ?= hackproof-runner:1

.DEFAULT_GOAL := help
.PHONY: help up down logs migrate test test-integration lint format fixtures drafts audits smoke fixture-remotes seed-demo runner-image runner-test prune env-check

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

# Builds and starts every fixture variant through the pipeline (M4: build and startup stages).
# FIXTURE_ARGS="--only F08" runs a subset; FIXTURE_ARGS=--list only lists the suite.
fixtures: env-check runner-image ## Run the whole fixture suite and compare with expected results
	$(COMPOSE) up -d --wait postgres buildkit
	$(RUN_WORKER) worker python -m scripts.run_fixtures $(FIXTURE_ARGS)

# Drafts every fixture's tests from its start text and checks the drafts (M6). By default the
# model's recorded replies in fixtures/llm are replayed: no network, no cost.
# DRAFTS_MODE=record calls the model and saves its replies. DRAFTS_ARGS="--only F01,D01" drafts
# a subset; DRAFTS_ARGS=--run also builds every variant and runs the drafted tests against it.
DRAFTS_MODE ?= replay
DRAFTS_RUN := $(findstring --run,$(DRAFTS_ARGS))
drafts: env-check $(if $(DRAFTS_RUN),runner-image) ## Draft the fixtures' tests from their start texts and check them
	$(if $(DRAFTS_RUN),$(COMPOSE) up -d --wait postgres buildkit)
	$(RUN_WORKER) $(RECORDINGS) -e LLM_MODE=$(DRAFTS_MODE) worker python -m scripts.run_drafts $(DRAFTS_ARGS)

# Audits every fixture variant from its own files, with Graphify's code map, and compares each
# claim's implementation with expected.json (M8). By default the model's recorded replies in
# fixtures/llm are replayed: no network, no cost. AUDITS_MODE=record calls the model and saves
# its replies. AUDITS_ARGS="--only F01,F09" audits a subset.
AUDITS_MODE ?= replay
audits: env-check ## Audit every fixture variant and compare with expected.json
	$(RUN_WORKER) $(RECORDINGS) -e LLM_MODE=$(AUDITS_MODE) worker python -m scripts.run_audits $(AUDITS_ARGS)

fixture-remotes: ## Build the fixture repositories into fixtures/remotes (on the host: the worker mounts them read-only)
	uv run python -m scripts.make_history_fixtures

# The M10 smoke test: a browser (the runner image's Chromium) walks the organiser's setup, a team's
# flow and a judge's brief against the running stack. Needs `make up` with LLM_MODE=replay and
# REPO_PROVIDER=local. It adds a "Smoke test" event to the stack's database. On a timeout it
# leaves the page's screenshot and HTML in SMOKE_OUT.
SMOKE_OUT ?= $(CURDIR)/.artifacts/smoke
smoke: env-check runner-image ## Browser smoke test: organiser, team and judge against the running stack
	mkdir -p "$(SMOKE_OUT)" && chmod 777 "$(SMOKE_OUT)"
	docker run --rm --add-host=host.docker.internal:host-gateway \
	  -e ORGANISER_PASSWORD="$$(grep -E '^ORGANISER_PASSWORD=' .env | cut -d= -f2-)" \
	  -v "$(SMOKE_OUT)":/out -e SMOKE_OUT=/out \
	  -v "$(CURDIR)/scripts/smoke.py":/smoke/smoke.py:ro --entrypoint python $(RUNNER_IMAGE) /smoke/smoke.py

seed-demo: env-check runner-image ## Create the demo event and pre-run every job
	$(COMPOSE) up -d --wait postgres buildkit
	$(RUN_WORKER) worker python -m scripts.seed_demo

runner-image: ## Build the test runner image
	docker build -f runner/Dockerfile --target runner -t $(RUNNER_IMAGE) .

# The UI runner's browser tests (M11): real Chromium against small local pages, in the runner image
# plus pytest. On the host the same tests are skipped, since Playwright lives only in the image.
runner-test: ## Browser tests of the UI runner, inside the runner image
	docker build -f runner/Dockerfile --target runner-test -t $(RUNNER_IMAGE)-test .
	docker run --rm --init --shm-size 512m -v "$(CURDIR)":/src:ro -w /src -e PYTHONPATH=/src \
	  --entrypoint python $(RUNNER_IMAGE)-test -m pytest -p no:cacheprovider --noconftest \
	  -o addopts= -W ignore::pytest.PytestConfigWarning -q tests/browser

prune: ## Remove HackProof run containers, networks and built team images
	@ids=$$(docker ps -aq --filter label=hackproof.attempt); [ -z "$$ids" ] || docker rm -f $$ids
	@nets=$$(docker network ls -q --filter label=hackproof.attempt); [ -z "$$nets" ] || docker network rm $$nets
	@imgs=$$(docker images --filter reference='hp/*' -q | sort -u); [ -z "$$imgs" ] || docker rmi -f $$imgs
	@echo "pruned"
