# KnowledgeBase stack: Graphiti REST + Neo4j + Open WebUI. Ollama is external.
# All config lives in .env; secrets live in .env.local (gitignored).
# Run `make` or `make help` to list targets.

.DEFAULT_GOAL := help

COMPOSE  := docker compose
DATA_DIR := ./data

.PHONY: help bootstrap preflight pull pull-models start stop restart logs ps config \
        health test test-e2e api-keys admin-signup rag-config \
        gdrive-sync gdrive-index-bootstrap gdrive-status \
        shell-owui shell-neo4j shell-graphiti shell-caddy clear clear-all clean

help: ## Show this help
	@awk 'BEGIN {FS=":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

bootstrap: ## Create .env.local (generate WEBUI_SECRET_KEY) + ./data dirs
	@./scripts/bootstrap.sh

preflight: ## Read-only checks: docker, secrets, Ollama, required models
	@./scripts/preflight.sh

pull: ## Pull all images
	@$(COMPOSE) pull

pull-models: ## Pull base LLM, create the ctx-baked variant (MODEL_NAME), pull embedder
	@set -euo pipefail; \
	set -a; . ./.env; set +a; \
	: "$${OLLAMA_MODEL_BASE:?OLLAMA_MODEL_BASE not set in .env}"; \
	: "$${MODEL_NAME:?MODEL_NAME not set in .env}"; \
	case "$${OLLAMA_MODEL_CONTEXT:-}" in ''|*[!0-9]*) \
	  echo "REFUSING: OLLAMA_MODEL_CONTEXT must be a positive integer (got '$${OLLAMA_MODEL_CONTEXT:-<unset>}')" >&2; exit 1;; esac; \
	[ "$$OLLAMA_MODEL_CONTEXT" -gt 0 ] || { echo "REFUSING: OLLAMA_MODEL_CONTEXT must be > 0" >&2; exit 1; }; \
	echo "Pulling base LLM: $$OLLAMA_MODEL_BASE"; ollama pull "$$OLLAMA_MODEL_BASE"; \
	mf=$$(mktemp); printf 'FROM %s\nPARAMETER num_ctx %s\n' "$$OLLAMA_MODEL_BASE" "$$OLLAMA_MODEL_CONTEXT" > $$mf; \
	echo "Creating ctx variant: $$MODEL_NAME (num_ctx=$$OLLAMA_MODEL_CONTEXT)"; \
	ollama rm "$$MODEL_NAME" >/dev/null 2>&1 || true; \
	ollama create "$$MODEL_NAME" -f $$mf; rm -f $$mf; \
	echo "Pulling embedder: nomic-embed-text"; ollama pull nomic-embed-text; \
	echo "Done. If the stack is running, restart it so Ollama reloads the new manifest: make restart"

start: ## Start the stack detached (run `make bootstrap` first)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@unset WEBUI_SECRET_KEY; \
	  . ./.env.local 2>/dev/null || { echo "MISSING secret — run: make bootstrap"; exit 1; }; \
	  [ -n "$${WEBUI_SECRET_KEY:-}" ] \
	    || { echo "MISSING secret — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local; set +a; \
	case "$$OLLAMA_HOST" in \
	  *'<ollama-host>'*) echo "REFUSING start: OLLAMA_HOST is still the '<ollama-host>' placeholder — set OLLAMA_HOST (shell env or .env) to the real Ollama URL"; exit 1;; \
	esac; \
	if [ -n "$${GDRIVE_KB_ID:-}" ]; then \
	  echo "gdrive indexer provisioned (GDRIVE_KB_ID set) — starting stack with --profile gdrive"; \
	  $(COMPOSE) --profile gdrive up -d; \
	else \
	  echo "gdrive indexer not provisioned (GDRIVE_KB_ID unset) — starting stack without it (run: make gdrive-index-bootstrap to add it)"; \
	  $(COMPOSE) up -d; \
	fi

stop: ## Stop the stack (keeps containers + data)
	@$(COMPOSE) stop

restart: stop start ## Restart (stop then start)

logs: ## Tail all service logs (Ctrl-C to detach)
	@$(COMPOSE) logs -f

ps: ## Show container status (with health)
	@$(COMPOSE) ps

config: ## Render effective compose config (secrets redacted)
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  $(COMPOSE) config | sed -E 's/(WEBUI_SECRET_KEY|OPENWEBUI_ADMIN_API_KEY|OPEN_WEBUI_API_KEY|OPENWEBUI_USER_API_KEY|OIKB_API_KEY|OPENWEBUI_USER_PASSWORD|OPENWEBUI_TEST_PASSWORD): .*/\1: <redacted>/'

health: ## Probe the stack /health (Caddy -> kb-gateway aggregated, reflects OWUI)
	@set -a; . ./.env; set +a; \
	H=$${KB_HOST:-http://localhost:$${KB_HOST_PORT:-3000}}; \
	curl -sf "$$H/health" >/dev/null \
	  && echo "stack healthy ($$H/health)" || { echo "stack DOWN ($$H/health)"; exit 1; }

test: ## Run system integration tests against the running stack (run: make start)
	@status=0; for t in tests/test_*.sh; do [ -e "$$t" ] || continue; \
	  echo; echo "=== $$t ==="; bash "$$t" || status=1; \
	done; exit $$status

test-e2e: ## DESTRUCTIVE: wipe + re-provision from scratch (incl. gdrive indexer) + full test suite + e2e
	@set -e; \
	echo "==> DESTRUCTIVE: wipes all data and re-provisions from scratch."; \
	test -f .env.local || { echo "REFUSING: no .env.local (no admin creds to stash) — run make bootstrap + fill OPENWEBUI_TEST_USER/PASSWORD first" >&2; exit 1; }; \
	set -a; . ./.env; . ./.env.local; set +a; \
	[ -n "$${OPENWEBUI_TEST_USER:-}" ] && [ -n "$${OPENWEBUI_TEST_PASSWORD:-}" ] \
	  || { echo "REFUSING: OPENWEBUI_TEST_USER/PASSWORD not set in .env.local (admin account) — fill them first" >&2; exit 1; }; \
	stash=$$(mktemp); chmod 600 $$stash; \
	{ printf 'OPENWEBUI_TEST_USER=%s\nOPENWEBUI_TEST_PASSWORD=%s\n' "$$OPENWEBUI_TEST_USER" "$$OPENWEBUI_TEST_PASSWORD"; \
	  [ -n "$${OPENWEBUI_USER:-}" ] && printf 'OPENWEBUI_USER=%s\n' "$$OPENWEBUI_USER" || true; } > $$stash; \
	trap 'rm -f $$stash' EXIT; \
	$(MAKE) clear-all && unset GDRIVE_KB_ID OIKB_API_KEY && \
	$(MAKE) bootstrap && \
	./scripts/e2e-restore-creds.sh $$stash && \
	$(MAKE) preflight && \
	$(MAKE) start && \
	{ H=$${KB_HOST:-http://localhost:$${KB_HOST_PORT:-3000}}; i=0; \
	  until curl -sf "$$H/health" >/dev/null; do i=$$((i+1)); [ $$i -lt 60 ] || { echo "stack did not become healthy in 120s ($$H/health)" >&2; exit 1; }; sleep 2; done; \
	  echo "stack healthy ($$H/health)"; } && \
	$(MAKE) admin-signup && \
	$(MAKE) api-keys && \
	$(MAKE) rag-config && \
	$(MAKE) gdrive-index-bootstrap && \
	./scripts/e2e-wait-indexer.sh && \
	$(MAKE) test && \
	echo "==> test-e2e PASS"

api-keys: ## Provision admin + agent-user API keys into .env.local (run after `make start` + admin signup)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_TEST_USER=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_TEST_USER/PASSWORD in .env.local (the admin account)"; exit 1; }
	@./scripts/api-keys.sh

admin-signup: ## Create the OWUI admin account (OPENWEBUI_TEST_USER/PASSWORD) via signup API; run after make start
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_TEST_USER=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_TEST_USER/PASSWORD in .env.local (the admin account)"; exit 1; }
	@./scripts/admin-signup.sh

rag-config: ## Set the strict-grounding RAG template in Open WebUI (run after `make api-keys`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local 	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/rag-config.sh

gdrive-sync: ## Sync all shared-drive files into ./gdrive (delta; deleted/overwritten retained in ./.gdrive-backup)
	@./scripts/gdrive-sync

gdrive-index-bootstrap: ## Create the OWUI "gdrive" KB + grant agent read + start the indexer (run after `make api-keys`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@grep -qE '^OPENWEBUI_USER_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_USER_API_KEY in .env.local (the read-scoped agent key; run: make api-keys)"; exit 1; }
	@./scripts/gdrive-index-bootstrap.sh

gdrive-status: ## Show gdrive RAG indexing status (indexed vs source, ETA if syncing)
	@./scripts/gdrive-status.sh

shell-owui: ## Shell into the Open WebUI container
	@docker exec -it kb-openwebui sh

shell-neo4j: ## Shell into the Neo4j container
	@docker exec -it kb-neo4j bash

shell-graphiti: ## Shell into the graphiti container
	@docker exec -it kb-graphiti sh

shell-caddy: ## Shell into the Caddy gateway container
	@docker exec -it kb-graphiti-gateway sh

clear: ## Teardown: stop + remove containers + network. KEEPS ./data and .env.local.
	@$(COMPOSE) down --remove-orphans

clear-all: ## Full wipe: clear + DELETE ./data + ./.gdrive-backup + remove .env.local. Keeps .env and configs.
	@$(COMPOSE) down --remove-orphans --volumes
	@# Remove ./data as root via a throwaway container: OWUI (root) and Neo4j
	@# (neo4j uid) write bind-mount files the host user cannot delete, so a host
	@# `rm -rf` fails midway and never reaches `rm -f .env.local`.
	@docker run --rm -v "$(CURDIR)/$(DATA_DIR):/data" alpine sh -c "rm -rf /data/*"
	@rm -f .env.local
	@rm -rf ./.gdrive-backup
	@echo "Wiped containers, ./data, ./.gdrive-backup, and .env.local. .env, graphiti/config.yaml, caddy/Caddyfile are preserved."

clean: ## Remove the rclone --backup-dir retention tree (./.gdrive-backup). Non-destructive: does not touch the stack, ./data, or .env.local.
	@rm -rf ./.gdrive-backup
	@echo "Removed ./.gdrive-backup (rclone sync backup retention)."
