# KnowledgeBase stack: Graphiti REST + Neo4j + Open WebUI. Ollama is external.
# All config lives in .env; secrets live in .env.local (gitignored).
# Run `make` or `make help` to list targets.

.DEFAULT_GOAL := help

COMPOSE  := docker compose
DATA_DIR := ./data

.PHONY: help bootstrap preflight pull pull-models start stop restart logs ps config \
        health test test-e2e api-keys admin-signup rag-config \
        ocr-bootstrap ocr-config ocr-disable \
        gdrive-sync gdrive-index gdrive-index-bootstrap gdrive-status \
        shell-owui shell-neo4j shell-graphiti shell-caddy clean clean-all clean-backup

help: ## Show this help
	@awk 'BEGIN {FS=":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

bootstrap: ## Create .env.local (generate WEBUI_SECRET_KEY) + ./data dirs
	@./scripts/bootstrap.sh

preflight: ## Read-only checks: docker, secrets, Ollama, required models
	@./scripts/preflight.sh

pull: ## Pull all images
	@$(COMPOSE) pull

pull-models: ## Pull base LLM, create the ctx-baked variant (MODEL_NAME), pull embedder
	@./scripts/pull-models.sh

start: ## Start the stack detached (run `make bootstrap` first)
	@./scripts/start.sh

stop: ## Stop the stack (keeps containers + data)
	@$(COMPOSE) stop

restart: stop start ## Restart (stop then start)

logs: ## Tail all service logs (Ctrl-C to detach)
	@$(COMPOSE) logs -f

ps: ## Show container status (with health)
	@$(COMPOSE) ps

config: ## Render effective compose config (secrets redacted)
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  $(COMPOSE) config | sed -E 's/(WEBUI_SECRET_KEY|OPENWEBUI_ADMIN_API_KEY|OPEN_WEBUI_API_KEY|OPENWEBUI_USER_API_KEY|OCR_SERVICE_TOKEN|OPENWEBUI_USER_PASSWORD|OPENWEBUI_TEST_PASSWORD): .*/\1: <redacted>/'

health: ## Probe the stack /health (Caddy -> kb-gateway aggregated, reflects OWUI)
	@set -a; . ./.env; set +a; \
	H=$${KB_HOST:-http://localhost:$${KB_HOST_PORT:-3000}}; \
	curl -sf "$$H/health" >/dev/null \
	  && echo "stack healthy ($$H/health)" || { echo "stack DOWN ($$H/health)"; exit 1; }

test: ## Run system integration tests against the running stack (run: make start)
	@status=0; for t in tests/test_*.sh; do [ -e "$$t" ] || continue; \
	  echo; echo "=== $$t ==="; bash "$$t" || status=1; \
	done; exit $$status

test-e2e: ## DESTRUCTIVE: wipe + re-provision from scratch (incl. OCR engine + gdrive index) + full test suite + e2e
	@./scripts/test-e2e.sh

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

ocr-bootstrap: ## Build + start markitdown-ocr, point OWUI at it, set MARKITDOWN_OCR_PROVISIONED=1 (run after `make api-keys`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/ocr-bootstrap.sh

ocr-config: ## Set OWUI CONTENT_EXTRACTION_ENGINE=external -> markitdown-ocr (run after `make ocr-bootstrap`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@grep -qE '^OCR_SERVICE_TOKEN=.+$$' .env.local \
	  || { echo "MISSING OCR_SERVICE_TOKEN in .env.local (run: make ocr-bootstrap)"; exit 1; }
	@./scripts/ocr-config.sh enable

ocr-disable: ## Clear the external extraction engine + remove the marker + recreate openwebui (no KB reset)
	@./scripts/ocr-disable.sh

gdrive-sync: ## Sync all shared-drive files into ./gdrive (delta; deleted/overwritten retained in ./.gdrive-backup), then POST /index to reconcile into the OWUI gdrive KB. Use --index-all for a full re-index.
	@./scripts/gdrive-sync

gdrive-index: ## Reconcile ./gdrive into the OWUI gdrive KB via kb-gateway POST /index (admin; incremental). Set INDEX_ALL=1 for a full re-index.
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  H=$${KB_HOST:-http://localhost:$${KB_HOST_PORT:-3000}}; \
	  [ -n "$${GDRIVE_KB_ID:-}" ] || { echo "MISSING GDRIVE_KB_ID in .env.local (run: make gdrive-index-bootstrap)"; exit 1; }; \
	  [ -n "$${OPENWEBUI_ADMIN_API_KEY:-}" ] || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }; \
	  q="source=gdrive&kb_id=$$GDRIVE_KB_ID"; [ "$${INDEX_ALL:-0}" = "1" ] && q="$$q&reindex_all=1"; \
	  curl -sS --max-time 1200 -X POST "$$H/index?$$q" \
	    -H "Authorization: Bearer $$OPENWEBUI_ADMIN_API_KEY" \
	    -H "Content-Type: application/json" -d '{}'; echo

gdrive-index-bootstrap: ## Create the OWUI "gdrive" KB + grant agent read + write GDRIVE_KB_ID to .env.local (run after `make api-keys`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@grep -qE '^OPENWEBUI_USER_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_USER_API_KEY in .env.local (the read-scoped agent key; run: make api-keys)"; exit 1; }
	@./scripts/gdrive-index-bootstrap.sh

gdrive-status: ## Show gdrive index status via kb-gateway GET /status (completed/pending/processing/failed)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  H=$${KB_HOST:-http://localhost:$${KB_HOST_PORT:-3000}}; \
	  [ -n "$${GDRIVE_KB_ID:-}" ] || { echo "MISSING GDRIVE_KB_ID in .env.local (run: make gdrive-index-bootstrap)"; exit 1; }; \
	  [ -n "$${OPENWEBUI_USER_API_KEY:-}" ] || { echo "MISSING OPENWEBUI_USER_API_KEY in .env.local (run: make api-keys)"; exit 1; }; \
	  curl -sS "$$H/status?source=gdrive&kb_id=$$GDRIVE_KB_ID" \
	    -H "Authorization: Bearer $$OPENWEBUI_USER_API_KEY"; echo

shell-owui: ## Shell into the Open WebUI container
	@docker exec -it kb-openwebui sh

shell-neo4j: ## Shell into the Neo4j container
	@docker exec -it kb-neo4j bash

shell-graphiti: ## Shell into the graphiti container
	@docker exec -it kb-graphiti sh

shell-caddy: ## Shell into the Caddy gateway container
	@docker exec -it kb-graphiti-gateway sh

clean: ## Teardown: stop + remove containers + network. KEEPS ./data and .env.local.
	@$(COMPOSE) down --remove-orphans

clean-all: ## Full wipe: clean + DELETE ./data + ./.gdrive-backup + remove .env.local. Keeps .env and configs.
	@$(COMPOSE) down --remove-orphans --volumes
	@# Remove ./data as root via a throwaway container: OWUI (root) and Neo4j
	@# (neo4j uid) write bind-mount files the host user cannot delete, so a host
	@# `rm -rf` fails midway and never reaches `rm -f .env.local`.
	@docker run --rm -v "$(CURDIR)/$(DATA_DIR):/data" alpine sh -c "rm -rf /data/*"
	@rm -f .env.local
	@rm -rf ./.gdrive-backup
	@echo "Wiped containers, ./data, ./.gdrive-backup, and .env.local. .env, graphiti/config.yaml, caddy/Caddyfile are preserved."

clean-backup: ## Remove the rclone --backup-dir retention tree (./.gdrive-backup). Non-destructive: does not touch the stack, ./data, or .env.local.
	@rm -rf ./.gdrive-backup
	@echo "Removed ./.gdrive-backup (rclone sync backup retention)."
