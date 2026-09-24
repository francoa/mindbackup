# Mirrors the justfile for machines without `just`.
# Run `make` (or `make help`) to list targets.

.SILENT:
.DEFAULT_GOAL := help

DOCKER_COMPOSE_FILE := -f docker-compose.yml
SERVICE_NAME := mindbackup
USER_SPECIFICATION := --user $$(id -u)

DOCKER_COMPOSE := $(shell command -v docker-compose >/dev/null 2>&1 && echo docker-compose || echo docker compose)

.PHONY: help build manual-ingest doctor extract-dry-run extract-interactive extract ask \
	delete-memo start stop bash bash-root test lint format

help: ## List available targets
	grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-20s %s\n", $$1, $$2}'

build: ## Build the service image
	$(DOCKER_COMPOSE) $(DOCKER_COMPOSE_FILE) build $(SERVICE_NAME)

manual-ingest: ## Ingest an audio file (prompts for path and name)
	echo "Input audio path (without file name)" && read path && echo "Input audio file name" && read name && $(DOCKER_COMPOSE) run --rm -v $$path:/opt/audios $(SERVICE_NAME) ingest /opt/audios/$$name

doctor: ## Run the doctor check
	$(DOCKER_COMPOSE) run --rm $(SERVICE_NAME) doctor

extract-dry-run: ## Extract one memo without writing
	$(DOCKER_COMPOSE) run --rm $(SERVICE_NAME) extract --dry-run --limit 1

extract-interactive: ## Extract 2026-09-18_3.md interactively
	$(DOCKER_COMPOSE) run --rm $(SERVICE_NAME) extract 2026-09-18_3.md --interactive

extract: ## Extract all pending memos
	$(DOCKER_COMPOSE) run --rm $(SERVICE_NAME) extract

ask: ## Ask a question (prompts for query)
	echo "What do you want to know?" && read query && $(DOCKER_COMPOSE) run --rm $(SERVICE_NAME) ask $$query

# Admin: delete a memo plus its atoms and topic bullets. Asks before deleting.
# e.g. `make delete-memo MEMO=2026-09-18_3` or `make delete-memo MEMO=2026-09-18_3 FLAGS=--dry-run`
delete-memo: ## Delete a memo and what was filed from it (MEMO=... [FLAGS=...])
	test -n "$(MEMO)" || { echo "Usage: make delete-memo MEMO=<memo> [FLAGS=...]"; exit 1; }
	$(DOCKER_COMPOSE) run --rm $(SERVICE_NAME) delete $(MEMO) $(FLAGS)

start: ## Start the service
	$(DOCKER_COMPOSE) $(DOCKER_COMPOSE_FILE) up $(SERVICE_NAME)

stop: ## Stop the service
	$(DOCKER_COMPOSE) $(DOCKER_COMPOSE_FILE) down $(SERVICE_NAME)

bash: ## Open a shell in the running container as your user
	$(DOCKER_COMPOSE) $(DOCKER_COMPOSE_FILE) exec $(USER_SPECIFICATION) $(SERVICE_NAME) bash

bash-root: ## Open a root shell in the running container
	$(DOCKER_COMPOSE) $(DOCKER_COMPOSE_FILE) exec $(SERVICE_NAME) bash

test: ## Run the test suite
	uv run --extra dev pytest -q .

lint: ## Run ruff check
	uv run --extra dev ruff check src tests

# Not checked by `make lint`: the repo is not ruff-format clean yet, and
# running this reformats ~18 files in one go. Opt in deliberately.
format: ## Run ruff format and ruff check --fix
	uv run --extra dev ruff format src tests
	uv run --extra dev ruff check --fix src tests
