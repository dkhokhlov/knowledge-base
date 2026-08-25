# KnowledgeBase stack: Graphiti REST + Neo4j + Open WebUI. Ollama is external.
# All config lives in .env; secrets live in .env.local (gitignored).
# Run `make` or `make help` to list targets.

.DEFAULT_GOAL := help

COMPOSE  := docker compose
DATA_DIR := ./data

.PHONY: help provision bootstrap preflight pull pull-models start stop restart logs ps config \
        health test test-output test-e2e api-keys admin-signup rag-config \
        users-create users-list users-search \
        ocr-config \
        gdrive-sync gdrive-index gdrive-index-bootstrap gdrive-status \
        projects-bootstrap \
        shell-owui shell-neo4j shell-graphiti shell-caddy clean clean-all clean-backup

help: ## Show this help
	@awk 'BEGIN {FS=":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

provision: ## ONE-TIME from-scratch setup: bootstrap + pull-models + start + admin-signup + api-keys (auto OCR) + projects-bootstrap + rag-config + gdrive KB. Leaves the stack running.
	@set -e; \
	  echo "==> 1/8 bootstrap (creates .env/.env.local + secrets + ./data dirs)"; make bootstrap; \
	  echo "==> 2/8 pull-models (BLOCKING: pulls base LLM + ctx variant + embedder + deepseek-ocr from Ollama)"; make pull-models; \
	  echo "==> 3/8 start (preflight + docker compose up -d, + --profile ocr when OCR_ENABLED=true)"; make start; \
	  echo "==> waiting for stack /health (OWUI has a 40s start period)..."; \
	  _KB_DOMAIN_OVR=$${KB_DOMAIN:-}; _OCR_ENABLED_OVR=$${OCR_ENABLED:-}; \
	  set -a; . ./.env; set +a; \
	  [ -n "$$_KB_DOMAIN_OVR" ] && export KB_DOMAIN="$$_KB_DOMAIN_OVR"; \
	  [ -n "$$_OCR_ENABLED_OVR" ] && export OCR_ENABLED="$$_OCR_ENABLED_OVR"; \
	  H=$${KB_HOST:-http://localhost:$${KB_HOST_PORT:-3000}}; \
	  i=0; until curl -sf "$$H/health" >/dev/null 2>&1; do i=$$((i+1)); [ $$i -lt 60 ] \
	    || { echo "stack did not become healthy in 120s ($$H/health)" >&2; exit 1; }; sleep 2; done; \
	  echo "  stack healthy ($$H/health)"; \
	  echo "==> 4/8 admin-signup (creates the admin@<KB_DOMAIN> account)"; make admin-signup; \
	  echo "==> 5/8 api-keys (admin + agent keys; auto-configures OWUI -> markitdown-ocr when OCR_ENABLED=true)"; make api-keys; \
	  echo "==> 6/8 projects-bootstrap (one-time admin enable of workspace.knowledge so user keys can create project-memory KBs)"; make projects-bootstrap; \
	  echo "==> 7/8 rag-config (strict-grounding RAG template + rag.ollama.base_url sync)"; make rag-config; \
	  echo "==> 8/8 gdrive-index-bootstrap (creates the gdrive KB + grants agent read + writes GDRIVE_KB_ID)"; make gdrive-index-bootstrap; \
	  echo; echo "==> provision complete — stack is running."; \
	  echo "    Populate the gdrive KB (one-time):           make gdrive-sync"; \
	  echo "    Everyday restart:                            make start"

bootstrap: ## Create .env.local (generate WEBUI_SECRET_KEY) + ./data dirs
	@./scripts/bootstrap.sh

preflight: ## Read-only checks: docker, secrets, Ollama, required models
	@./scripts/preflight.sh

pull: ## Pull all images
	@$(COMPOSE) pull

pull-models: ## Pull base LLM, create the ctx-baked variant (GRAPHITI_MODEL), pull embedder
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
	  $(COMPOSE) config | sed -E 's/(WEBUI_SECRET_KEY|OPENWEBUI_ADMIN_API_KEY|OPEN_WEBUI_API_KEY|OPENWEBUI_USER_API_KEY|OCR_SERVICE_TOKEN|OPENWEBUI_USER_PASSWORD|OPENWEBUI_FIRST_PASSWORD): .*/\1: <redacted>/'

health: ## Probe the stack /health (Caddy -> kb-gateway aggregated, reflects OWUI)
	@set -a; . ./.env; set +a; \
	H=$${KB_HOST:-http://localhost:$${KB_HOST_PORT:-3000}}; \
	curl -sf "$$H/health" >/dev/null \
	  && echo "stack healthy ($$H/health)" || { echo "stack DOWN ($$H/health)"; exit 1; }

test: ## Run unit tests (no stack) then system integration tests against the running stack (run: make start)
	@status=0; echo "=== unit: test_output_json ==="; \
	  python3 tests/test_output_json.py -v || status=1; \
	  for t in tests/test_*.sh; do [ -e "$$t" ] || continue; \
	  case "$$t" in *test_09_gdrive_index.sh) \
	    echo "==> skip $$t (full real-gdrive drain; run via: make test-e2e)"; continue;; esac; \
	  echo; echo "=== $$t ==="; bash "$$t" || status=1; \
	done; exit $$status

test-output: ## Unit-test CLI JSON output schemas (no stack needed)
	@python3 tests/test_output_json.py -v

test-e2e: ## DESTRUCTIVE: wipe + re-provision from scratch (incl. OCR engine + gdrive index) + full test suite + e2e
	@./scripts/test-e2e.sh

api-keys: ## Provision admin + agent-user API keys into .env.local (run after `make start` + admin signup)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_FIRST_USER=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_FIRST_USER/PASSWORD in .env.local (the admin account)"; exit 1; }
	@./scripts/api-keys.sh

admin-signup: ## Create the OWUI admin account (OPENWEBUI_FIRST_USER/PASSWORD) via signup API; run after make start
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_FIRST_USER=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_FIRST_USER/PASSWORD in .env.local (the admin account)"; exit 1; }
	@./scripts/admin-signup.sh

users-create: ## Create a new OWUI KB user (admin) via kb-gateway POST /admin/users. Set EMAIL=, NAME=, [ROLE=user]. Prints {email, temp_password, kb_api_key, role, id} as pretty JSON; relay temp_password + kb_api_key out-of-band.
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/users.sh create

users-list: ## List all OWUI users (admin) as pretty JSON (indent 2).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/users.sh list

users-search: ## Search OWUI users by name/email substring (admin). Set QUERY=. Pretty JSON (indent 2).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/users.sh search

rag-config: ## Set the strict-grounding RAG template in Open WebUI (run after `make api-keys`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local 	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/rag-config.sh

ocr-config: ## Re-assert OWUI CONTENT_EXTRACTION_ENGINE=external -> markitdown-ocr (auto-set by make api-keys when OCR_ENABLED=true; re-run after a DB reset)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@_OVR="$${OCR_ENABLED:-}"; set -a; . ./.env 2>/dev/null; set +a; \
	  if [ -n "$$_OVR" ]; then export OCR_ENABLED="$$_OVR"; fi; \
	  if [ "$${OCR_ENABLED:-true}" != "true" ]; then echo "OCR_ENABLED=$${OCR_ENABLED} — nothing to configure"; exit 0; fi; \
	  grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	    || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }; \
	  grep -qE '^OCR_SERVICE_TOKEN=.+$$' .env.local \
	    || { echo "MISSING OCR_SERVICE_TOKEN in .env.local (run: make bootstrap with OCR_ENABLED=true)"; exit 1; }; \
	  ./scripts/ocr-config.sh

gdrive-sync: ## Sync all shared-drive files into ./gdrive (delta; deleted/overwritten retained in ./.gdrive-backup), then POST /index to reconcile into the OWUI gdrive KB. Use --index-all for a full re-index. Set SCOPE_PATH=<relpath> to index only a subpath (FULL reconcile of that subpath; use a KB whose whole scope is that path).
	@./scripts/gdrive-sync $${SCOPE_PATH:+--path "$$SCOPE_PATH"}

gdrive-index: ## Reconcile ./gdrive into the OWUI gdrive KB via kb-gateway POST /index (admin; incremental). Self-heals FAILED files (delete + re-upload) by default. Set RETRY_PENDING=1 to also retry stalled PENDING files. Set INDEX_ALL=1 for a full re-index. Set SCOPE_PATH=<relpath> to index only a subpath (FULL reconcile of that subpath; use a KB whose whole scope is that path).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  H=$${KB_HOST:-http://localhost:$${KB_HOST_PORT:-3000}}; \
	  [ -n "$${GDRIVE_KB_ID:-}" ] || { echo "MISSING GDRIVE_KB_ID in .env.local (run: make gdrive-index-bootstrap)"; exit 1; }; \
	  [ -n "$${OPENWEBUI_ADMIN_API_KEY:-}" ] || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }; \
	  q="source=gdrive&kb_id=$$GDRIVE_KB_ID"; [ "$${INDEX_ALL:-0}" = "1" ] && q="$$q&reindex_all=1"; \
	  [ "$${RETRY_PENDING:-0}" = "1" ] && q="$$q&retry_pending=1"; \
	  [ -n "$${SCOPE_PATH:-}" ] && q="$$q&path=$$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$$SCOPE_PATH")"; \
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

gdrive-status: ## Show gdrive index status via kb-gateway GET /status (completed/pending/processing/failed), pretty JSON. Set SCOPE_PATH=<relpath> to scope source_count to a subpath (file counts are KB-wide; accurate when the KB's whole scope is that path).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  H=$${KB_HOST:-http://localhost:$${KB_HOST_PORT:-3000}}; \
	  [ -n "$${GDRIVE_KB_ID:-}" ] || { echo "MISSING GDRIVE_KB_ID in .env.local (run: make gdrive-index-bootstrap)"; exit 1; }; \
	  [ -n "$${OPENWEBUI_USER_API_KEY:-}" ] || { echo "MISSING OPENWEBUI_USER_API_KEY in .env.local (run: make api-keys)"; exit 1; }; \
	  q="source=gdrive&kb_id=$$GDRIVE_KB_ID"; [ -n "$${SCOPE_PATH:-}" ] && q="$$q&path=$$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$$SCOPE_PATH")"; \
	  curl -sS "$$H/status?$$q&json=1" \
	    -H "Authorization: Bearer $$OPENWEBUI_USER_API_KEY" \
	    | python3 -m json.tool --indent 2

projects-bootstrap: ## One-time admin enable of workspace.knowledge so the user key can create + own project-memory KBs (run after `make api-keys`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@grep -qE '^OPENWEBUI_USER_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_USER_API_KEY in .env.local (the caller key that owns project KBs; run: make api-keys)"; exit 1; }
	@./scripts/projects-index-bootstrap.sh

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

clean-all: ## Full wipe: clean + DELETE ./data + ./.gdrive-backup + backup-and-remove .env + .env.local. Keeps graphiti/config.yaml, caddy/Caddyfile, and the ./gdrive mirror.
	@$(COMPOSE) down --remove-orphans --volumes
	@# Remove ./data as root via a throwaway container: OWUI (root) and Neo4j
	@# (neo4j uid) write bind-mount files the host user cannot delete, so a host
	@# `rm -rf` fails midway and never reaches the config backup/wipe below.
	@docker run --rm -v "$(CURDIR)/$(DATA_DIR):/data" alpine sh -c "rm -rf /data/*"
	@# Backup .env + .env.local to a dated recovery dir, then remove them, so a
	@# bare `make provision` reprovisions from the .env.template default (pass
	@# `KB_DOMAIN=<d>` for a custom domain). `make clean-backup` clears the tree.
	@TS=$$(date -u +%Y%m%dT%H%M%SZ); mkdir -p ".config-backup/$$TS"; \
	  cp -p .env ".config-backup/$$TS/.env" 2>/dev/null || true; \
	  cp -p .env.local ".config-backup/$$TS/.env.local" 2>/dev/null || true; \
	  rm -f .env .env.local
	@rm -rf ./.gdrive-backup
	@echo "Wiped containers, ./data, ./.gdrive-backup, .env, .env.local (backed up to ./.config-backup/<TS>). graphiti/config.yaml, caddy/Caddyfile, ./gdrive preserved."

clean-backup: ## Remove the retention trees (./.gdrive-backup + ./.config-backup). Non-destructive: does not touch the stack, ./data, .env, or .env.local.
	@rm -rf ./.gdrive-backup ./.config-backup
	@echo "Removed ./.gdrive-backup + ./.config-backup (retention)."
