# KnowledgeBase stack: Graphiti MCP + Neo4j + Open WebUI. Ollama is external.
# All config lives in .env; secrets live in .env.local (gitignored).
# Run `make` or `make help` to list targets.

.DEFAULT_GOAL := help

COMPOSE  := docker compose
DATA_DIR := ./data

.PHONY: help bootstrap preflight pull pull-models start stop restart logs ps config \
        health test api-keys rag-config shell-owui shell-neo4j shell-graphiti shell-caddy clear clear-all

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
	@unset WEBUI_SECRET_KEY GRAPHITI_API_TOKEN; \
	  . ./.env.local 2>/dev/null || { echo "MISSING secret — run: make bootstrap"; exit 1; }; \
	  [ -n "$${WEBUI_SECRET_KEY:-}" ] && [ -n "$${GRAPHITI_API_TOKEN:-}" ] \
	    || { echo "MISSING secret — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; set +a; \
	case "$$OLLAMA_BASE_URL" in \
	  *'<ollama-host>'*) echo "REFUSING start: OLLAMA_BASE_URL is still the '<ollama-host>' placeholder — edit .env (cp .env.example .env, set the real Ollama host)"; exit 1;; \
	esac; \
	$(COMPOSE) up -d

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
	  && echo "graphiti-mcp healthy" || { echo "graphiti-mcp DOWN"; exit 1; }; \
	curl -sf http://localhost:$$OPENWEBUI_HOST_PORT/health >/dev/null \
	  && echo "open-webui healthy" || { echo "open-webui DOWN"; exit 1; }

test: ## Run system integration tests against the running stack (run: make start)
	@status=0; for t in tests/test_*.sh; do [ -e "$$t" ] || continue; \
	  echo; echo "=== $$t ==="; bash "$$t" || status=1; \
	done; exit $$status

api-keys: ## Provision admin + agent-user API keys into .env.local (run after `make start` + admin signup)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_TEST_USER=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_TEST_USER/PASSWORD in .env.local (the admin account)"; exit 1; }
	@./scripts/api-keys.sh

rag-config: ## Set the strict-grounding RAG template in Open WebUI (run after `make api-keys`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local 	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/rag-config.sh

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