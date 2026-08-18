# KnowledgeBase stack: Graphiti MCP + Neo4j + Open WebUI. Ollama is external.
# All config lives in .env; secrets live in .env.local (gitignored).
# Run `make` or `make help` to list targets.

.DEFAULT_GOAL := help

COMPOSE  := docker compose
DATA_DIR := ./data

.PHONY: help bootstrap preflight pull pull-models start stop restart logs ps config \
        health shell-owui shell-neo4j shell-graphiti shell-caddy clear clear-all

help: ## Show this help
	@awk 'BEGIN {FS=":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

bootstrap: ## Create .env.local (generate WEBUI_SECRET_KEY) + ./data dirs
	@./scripts/bootstrap.sh

preflight: ## Read-only checks: docker, secrets, Ollama, required models
	@./scripts/preflight.sh

pull: ## Pull all images
	@$(COMPOSE) pull

pull-models: ## Pull Ollama models (MODEL_NAME + nomic-embed-text) on the host
	@set -a; . ./.env; set +a; \
	echo "Pulling LLM model: $$MODEL_NAME"; ollama pull $$MODEL_NAME; \
	echo "Pulling embedder: nomic-embed-text"; ollama pull nomic-embed-text

start: ## Start the stack detached (run `make bootstrap` first)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^WEBUI_SECRET_KEY=.+$$' .env.local \
	  && grep -qE '^GRAPHITI_API_TOKEN=.+$$' .env.local \
	  || { echo "MISSING secret — set WEBUI_SECRET_KEY and GRAPHITI_API_TOKEN in .env.local (see make bootstrap)"; exit 1; }
	@$(COMPOSE) up -d

stop: ## Stop the stack (keeps containers + data)
	@$(COMPOSE) stop

restart: stop start ## Restart (stop then start)

logs: ## Tail all service logs (Ctrl-C to detach)
	@$(COMPOSE) logs -f

ps: ## Show container status (with health)
	@$(COMPOSE) ps

config: ## Render effective compose config (secrets redacted)
	@$(COMPOSE) config | sed -E 's/(GRAPHITI_API_TOKEN|WEBUI_SECRET_KEY): .*/\1: <redacted>/'

health: ## Probe graphiti /health and Open WebUI /health
	@set -a; . ./.env; set +a; \
	curl -sf http://localhost:$$GRAPHITI_HOST_PORT/health >/dev/null \
	  && echo "graphiti-mcp healthy" || (echo "graphiti-mcp DOWN"; exit 1); \
	curl -sf http://localhost:$$OPENWEBUI_HOST_PORT/health >/dev/null \
	  && echo "open-webui healthy" || (echo "open-webui DOWN"; exit 1)

shell-owui: ## Shell into the Open WebUI container
	@docker exec -it kb-openwebui sh

shell-neo4j: ## Shell into the Neo4j container
	@docker exec -it kb-neo4j bash

shell-graphiti: ## Shell into the graphiti-mcp container
	@docker exec -it kb-graphiti-mcp sh

shell-caddy: ## Shell into the Caddy gateway container
	@docker exec -it kb-graphiti-gateway sh

clear: ## Teardown: stop + remove containers + network. KEEPS ./data and .env.local.
	@$(COMPOSE) down --remove-orphans

clear-all: ## Full wipe: clear + DELETE ./data + remove .env.local. Keeps .env and configs.
	@$(COMPOSE) down --remove-orphans --volumes
	@rm -rf $(DATA_DIR)
	@rm -f .env.local
	@echo "Wiped containers, ./data, and .env.local. .env, graphiti/config.yaml, caddy/Caddyfile are preserved."