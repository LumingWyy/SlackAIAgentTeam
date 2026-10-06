# SlackAgentTeam — run `make help` for the list
.DEFAULT_GOAL := help
COMPOSE := docker compose
VENV := .venv/bin/python
NODE ?= alice
COMPOSE_NODE := $(COMPOSE) --profile $(NODE)

.PHONY: help build up down restart logs ps shell test test-docker run webui stop stop-run stop-webui clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

## --- Docker ops (prefer this for production start) ---

build: ## Build the image
	$(COMPOSE_NODE) build $(NODE)

up: ## Start one owner node (NODE=alice|bob). Always rebuild
	$(COMPOSE_NODE) up -d --build $(NODE)
	@echo "✅ started. logs: make logs / monitor: make webui"

down: ## Stop and remove
	$(COMPOSE_NODE) down

restart: ## Restart containers (agents.yaml / .env only; code changes need make up rebuild)
	$(COMPOSE_NODE) restart $(NODE)

logs: ## Follow logs
	$(COMPOSE_NODE) logs -f $(NODE)

ps: ## Process status
	$(COMPOSE_NODE) ps

shell: ## Shell into the container
	$(COMPOSE_NODE) exec $(NODE) bash

test-docker: build ## Run tests inside the container (image verification)
	$(COMPOSE_NODE) run --rm --no-deps $(NODE) python -m pytest tests/ -q

## --- Local development (venv) ---

test: ## Run tests locally
	$(VENV) -m pytest tests/ -q

run: ## Start locally (development)
	$(VENV) multi_app.py

webui: ## Start console (monitor + config) at http://127.0.0.1:8765
	$(VENV) webui.py

# The patterns are written [m]ulti_app / [w]ebui so they do not match the
# shell running this recipe, whose command line contains the pattern itself.
# Docker nodes run inside the Docker VM and are not affected (use make down).
stop: stop-run stop-webui ## Stop the local multi_app and console (foreground or background)

stop-run: ## Stop the local multi_app
	@if pkill -f '[m]ulti_app\.py$$'; then \
	  for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -f '[m]ulti_app\.py$$' >/dev/null || break; sleep 0.5; done; \
	  if pgrep -f '[m]ulti_app\.py$$' >/dev/null; then echo "multi_app is still shutting down"; else echo "stopped multi_app"; fi; \
	else echo "multi_app was not running"; fi

stop-webui: ## Stop the local console
	@if pkill -f '[w]ebui\.py$$'; then \
	  for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -f '[w]ebui\.py$$' >/dev/null || break; sleep 0.5; done; \
	  if pgrep -f '[w]ebui\.py$$' >/dev/null; then echo "webui is still shutting down"; else echo "stopped webui"; fi; \
	else echo "webui was not running"; fi

admin-check: ## Hit the running process admin API for status
	@curl -s http://127.0.0.1:8766/state | python3 -m json.tool || echo "multi_app not running, or admin API unreachable"

clean: ## Clean caches and logs
	rm -rf __pycache__ tests/__pycache__ .pytest_cache *.log *.pid
