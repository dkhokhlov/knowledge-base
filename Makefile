# KnowledgeBase stack: Graphiti REST + Neo4j + Open WebUI. Ollama is external.
# All config lives in .env; secrets live in .env.local (gitignored).
# Run `make` or `make help` to list targets.

.DEFAULT_GOAL := help

# COMPOSE_PROFILES (the ocr sidecar profile) is read by `docker compose` from
# .env for EVERY command, so stop/down/clean/pull/ps/config target the same
# service set as `make start` -- the markitdown-ocr sidecar is always in the
# project when OCR_ENABLED=true. `env -u COMPOSE_PROFILES` shields lifecycle
# calls from a stale COMPOSE_PROFILES in the operator's shell, so .env is
# strictly the source. To disable OCR: `make clean-all && make provision
# OCR_ENABLED=false` (make bootstrap bakes OCR_ENABLED=false + an empty
# COMPOSE_PROFILES into .env). No --profile flag, no shell export.
COMPOSE  := env -u COMPOSE_PROFILES docker compose
DATA_DIR := ./data
# pytest runs in the .venv provisioned by `make ci` (uv sync: Python 3.12 from
# .python-version + locked deps from uv.lock). The venv path avoids relying on a
# `pytest` binary on PATH. Override with PYTEST="python3 -m pytest" to use a
# different interpreter (still needs pytest installed in it).
PYTEST   ?= .venv/bin/python -m pytest

.PHONY: help provision bootstrap preflight pull pull-models start stop restart logs ps config \
        health ci test test-unit test-e2e test-e2e-long test-output test-e2e-iso api-keys admin-signup rag-config \
        users-create users-list users-search \
        ocr-config \
        gdrive-sync gdrive-index gdrive-index-bootstrap gdrive-status \
        kb-public-read kb-check \
        projects-bootstrap \
        shell-owui shell-neo4j shell-graphiti shell-caddy clean clean-all clean-test clean-tests clean-backup backup

help: ## Show this help
	@awk 'BEGIN {FS=":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

provision: ## ONE-TIME from-scratch setup: bootstrap + pull-models + start + admin-signup + api-keys (auto OCR) + projects-bootstrap + rag-config + gdrive KB. Leaves the stack running.
	@set -e; \
	  echo "==> 1/8 bootstrap (creates .env/.env.local + secrets + ./data dirs)"; make bootstrap; \
	  echo "==> 2/8 pull-models (BLOCKING: pulls base LLM + ctx variant + embedder + deepseek-ocr from Ollama)"; make pull-models; \
	  echo "==> 3/8 start (preflight + docker compose up -d; ocr sidecar via COMPOSE_PROFILES=ocr in .env)"; make start; \
	  echo "==> waiting for stack /health (OWUI has a 40s start period)..."; \
	  _KB_DOMAIN_OVR=$${KB_DOMAIN:-}; _OCR_ENABLED_OVR=$${OCR_ENABLED:-}; \
	  set -a; . ./.env; set +a; \
	  [ -n "$$_KB_DOMAIN_OVR" ] && export KB_DOMAIN="$$_KB_DOMAIN_OVR"; \
	  [ -n "$$_OCR_ENABLED_OVR" ] && export OCR_ENABLED="$$_OCR_ENABLED_OVR"; \
	  H=$${KB_HOST:?KB_HOST not set -- export KB_HOST=http://host:port (see .env.template)}; \
	  i=0; until curl -sf "$$H/health" >/dev/null 2>&1; do i=$$((i+1)); [ $$i -lt 60 ] \
	    || { echo "stack did not become healthy in 120s ($$H/health)" >&2; exit 1; }; sleep 2; done; \
	  echo "  stack healthy ($$H/health)"; \
	  echo "==> 4/8 admin-signup (creates the admin@<KB_DOMAIN> account)"; make admin-signup; \
	  echo "==> 5/8 api-keys (admin + agent keys; auto-configures OWUI -> markitdown-ocr when OCR_ENABLED=true)"; make api-keys; \
	  echo "==> 6/8 projects-bootstrap (one-time admin enable of workspace.knowledge + sharing.public_knowledge so user keys can create + publicly share project-memory KBs)"; make projects-bootstrap; \
	  echo "==> 7/8 rag-config (strict-grounding RAG template + rag.ollama.base_url sync)"; make rag-config; \
	  echo "==> 8/8 gdrive-index-bootstrap (creates the gdrive KB + grants public read + writes GDRIVE_KB_ID)"; make gdrive-index-bootstrap; \
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

stop: ## Stop the stack (keeps containers + data; stops the ocr sidecar too via COMPOSE_PROFILES=ocr in .env)
	@$(COMPOSE) stop

restart: stop start ## Restart (stop then start)

logs: ## Tail all service logs incl. the ocr sidecar (Ctrl-C to detach; via COMPOSE_PROFILES=ocr in .env)
	@$(COMPOSE) logs -f

ps: ## Show container status (with health)
	@$(COMPOSE) ps

config: ## Render effective compose config incl. the ocr sidecar (COMPOSE_PROFILES=ocr in .env; secrets redacted)
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  $(COMPOSE) config | sed -E 's/(WEBUI_SECRET_KEY|OPENWEBUI_ADMIN_API_KEY|OPEN_WEBUI_API_KEY|OPENWEBUI_USER_API_KEY|OCR_SERVICE_TOKEN|OPENWEBUI_USER_PASSWORD|OPENWEBUI_FIRST_PASSWORD): .*/\1: <redacted>/'

health: ## Probe the stack /health (Caddy -> api-gateway aggregated, reflects OWUI)
	@set -a; . ./.env; set +a; \
	H=$${KB_HOST:?KB_HOST not set -- export KB_HOST=http://host:port (see .env.template)}; \
	curl -sf "$$H/health" >/dev/null \
	  && echo "stack healthy ($$H/health)" || { echo "stack DOWN ($$H/health)"; exit 1; }

ci: ## Provision the .venv: uv sync (Python 3.12 from .python-version + locked deps from uv.lock). Idempotent; prereq for every test target.
	uv sync

test: ci ## Run the fast set: python unit tests + live-stack integration tests (excludes e2e + long). Requires: make start (for the integration tests).
	@$(PYTEST) -m "not e2e and not long" -v

test-unit: ci ## Run only the python unit tests (no stack needed).
	@$(PYTEST) -m unit -v

test-e2e: ci ## Run the quick isolated e2e tests (self-isolate a throwaway stack via scripts/e2e-env.sh; NOT the live stack). GPU/RAM: a 2nd stack on the shared Ollama.
	@$(PYTEST) -m "e2e and not long" -v

test-e2e-long: ci ## Run the long isolated e2e (test_08 agent-surface + test-e2e-iso at-scale). Self-isolate; GPU/RAM heavy; runs many minutes.
	@$(PYTEST) -m "e2e and long" -v
test-output: ci ## Unit-test CLI JSON output schemas (no stack needed)
	@.venv/bin/python tests/test_output_json.py -v

test-e2e-iso: ## Isolated e2e: clone to a datetime-stamped gitignored .test-e2e/<stamp>/ + run the destructive e2e (clean-state wipe + re-provision + rclone + full suite + test_09 drain) under a separate compose project (kb-e2e-<stamp>) so the LIVE stack keeps running. The destructive logic is inlined; there is NO in-place `make test-e2e` (it would wipe the live stack). REAL rclone (re-downloads the corpus). Set E2E_PORT (default 3010), OCR_ENABLED, E2E_KEEP=1. Costs: 2nd stack (GPU/RAM contention on the shared Ollama). On success docker is stopped but the clone is KEPT (proliferation -- may hold commits); flush with `make clean-tests`. On failure the stack + clone are left; run `make clean-test STAMP=<stamp>`.
	@./scripts/test-e2e-iso.sh

clean-test: ## Tear down ONE isolated e2e run + remove its clone. NAME=<name> (default e2e) + optional STAMP=<stamp> (latest stamp under .test-<name>/ if unset). Safe anytime (no-op if absent). Delegates to scripts/e2e-env.sh (shared with make test-e2e-iso + tests/test_*_e2e.sh).
	@bash -c '. scripts/e2e-env.sh; e2e_down "$${NAME:-e2e}" "$${STAMP:-}"'

clean-tests: ## Manual hygiene flush: remove EVERY .test-*/<stamp>/ clone + legacy un-stamped clones + stranded stamped e2e docker. Prints each clone's HEAD + unmerged commits before removing (a warning, not a hard refuse). NAME=<name> flushes only .test-<name>/. Run periodically -- stamped clones accumulate per e2e run (no autoclean). Delegates to scripts/e2e-env.sh.
	@bash -c '. scripts/e2e-env.sh; e2e_clean_tests "$${NAME:-}"'

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

users-create: ## Create a new OWUI KB user (admin) via api-gateway POST /admin/users. Set EMAIL=, NAME=, [ROLE=user]. Prints {email, temp_password, kb_api_key, role, id} as pretty JSON; relay temp_password + kb_api_key out-of-band.
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

gdrive-meta: ## Generate per-file .meta YAML sidecars (Drive description, [labels], file attributes, approval) next to each synced gdrive file (read-only; reuses rclone token; never prints credentials). Set FILE=<id> for one file, DRIVE=<name> for one drive, DRY_RUN=1 to preview. .meta sidecars are excluded from indexing and protected from sync deletion (gdrive-exclude.conf [*] *.meta).
	@./scripts/gdrive-meta.py $${FILE:+--file "$$FILE"} $${DRIVE:+--drive "$$DRIVE"} $${DRY_RUN:+--dry-run}

gdrive-index: ## Reconcile ./gdrive into the OWUI gdrive KB via api-gateway POST /index (admin; incremental). Self-heals FAILED files (delete + re-upload) by default. Set RETRY_PENDING=1 to also retry stalled PENDING files. Set INDEX_ALL=1 for a full re-index. Set SCOPE_PATH=<relpath> to index only a subpath (FULL reconcile of that subpath; use a KB whose whole scope is that path).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  H=$${KB_HOST:?KB_HOST not set -- export KB_HOST=http://host:port (see .env.template)}; \
	  [ -n "$${GDRIVE_KB_ID:-}" ] || { echo "MISSING GDRIVE_KB_ID in .env.local (run: make gdrive-index-bootstrap)"; exit 1; }; \
	  [ -n "$${OPENWEBUI_ADMIN_API_KEY:-}" ] || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }; \
	  q="source=gdrive&kb_id=$$GDRIVE_KB_ID"; [ "$${INDEX_ALL:-0}" = "1" ] && q="$$q&reindex_all=1"; \
	  [ "$${RETRY_PENDING:-0}" = "1" ] && q="$$q&retry_pending=1"; \
	  [ -n "$${SCOPE_PATH:-}" ] && q="$$q&path=$$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$$SCOPE_PATH")"; \
	  curl -sS --max-time 1200 -X POST "$$H/index?$$q" \
	    -H "Authorization: Bearer $$OPENWEBUI_ADMIN_API_KEY" \
	    -H "Content-Type: application/json" -d '{}'; echo

gdrive-index-bootstrap: ## Create the OWUI "gdrive" KB + grant public read (user:*) + write GDRIVE_KB_ID to .env.local (run after `make api-keys`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/gdrive-index-bootstrap.sh

kb-public-read: ## Grant public read (user:*) on EVERY knowledge base + enable sharing.public_knowledge so all authenticated users read all KBs (admin). Backfills existing KBs; re-run as a safety net for KBs created outside the flows (e.g. via the OWUI UI).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/kb-public-read.sh

kb-check: ## Cross-DB health check (OWUI SQLite + Chroma): audit both DBs, report 12 inconsistency classes, advise purge. PURGE=1 to purge safe classes (1 ghosts, 3 orphan file-{id}, 11 dangling dirs; BACKUP=1 default exports first). PURGE=1 MAINT=1 stops OWUI to also purge maint classes (5b leaked KB vectors, 7 orphan junction, 8 dead-KB junction). REPAIR=1 stops OWUI to repair class-9 stuck-processing-while-linked files (linked + content + vectors, but status stuck at processing) -> completed; combine with PURGE/MAINT to do both. KB=<id> scopes the KB-tagged classes; JSON=1 machine-readable; SHOW_NAMES=1 prints filenames (default ids-only).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  OWUI="$${OWUI_CONTAINER:-kb-openwebui}"; \
	  if [ "$${MAINT:-0}" = "1" ] || [ "$${REPAIR:-0}" = "1" ]; then \
	    echo "==> maintenance window: stopping $$OWUI (direct Chroma/SQLite writes)"; \
	    docker stop $$OWUI >/dev/null; \
	    trap 'echo "==> restarting $$OWUI"; docker start $$OWUI >/dev/null' EXIT; \
	    docker run --rm --entrypoint /usr/local/bin/python3 \
	      -v "$$(readlink -f "$${DATA_ROOT:-./data}")/openwebui:/app/backend/data" \
	      -v "$(CURDIR)/scripts/kb_check.py:/app/kb_check.py:ro" \
	      ghcr.io/dkhokhlov/open-webui:"$${OPENWEBUI_IMAGE_TAG:?OPENWEBUI_IMAGE_TAG required in .env}" \
	      /app/kb_check.py $${KB:+--kb $$KB} $${JSON:+--json} $${SHOW_NAMES:+--show-names} \
	        $${PURGE:+--purge} $${MAINT:+--maint} $${REPAIR:+--repair} \
	        $$( [ "$${BACKUP:-1}" = "0" ] && echo --no-backup ); \
	  else \
	    KEY_ENV=; if [ "$${PURGE:-0}" = "1" ]; then \
	      [ -n "$${OPENWEBUI_ADMIN_API_KEY:-}" ] || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (PURGE=1 ghost delete needs it)"; exit 1; }; \
	      KEY_ENV="-e OPENWEBUI_ADMIN_API_KEY"; fi; \
	    docker exec -i $$KEY_ENV $$OWUI python3 - < scripts/kb_check.py \
	      $${KB:+--kb $$KB} $${JSON:+--json} $${SHOW_NAMES:+--show-names} \
	      $${PURGE:+--purge} $$( [ "$${BACKUP:-1}" = "0" ] && echo --no-backup ); \
	  fi

gdrive-status: ## Show gdrive index status via api-gateway GET /status (completed/pending/processing/failed), pretty JSON. Set SCOPE_PATH=<relpath> to scope source_count to a subpath (file counts are KB-wide; accurate when the KB's whole scope is that path).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  H=$${KB_HOST:?KB_HOST not set -- export KB_HOST=http://host:port (see .env.template)}; \
	  [ -n "$${GDRIVE_KB_ID:-}" ] || { echo "MISSING GDRIVE_KB_ID in .env.local (run: make gdrive-index-bootstrap)"; exit 1; }; \
	  [ -n "$${OPENWEBUI_USER_API_KEY:-}" ] || { echo "MISSING OPENWEBUI_USER_API_KEY in .env.local (run: make api-keys)"; exit 1; }; \
	  q="source=gdrive&kb_id=$$GDRIVE_KB_ID"; [ -n "$${SCOPE_PATH:-}" ] && q="$$q&path=$$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$$SCOPE_PATH")"; \
	  curl -sS "$$H/status?$$q&json=1" \
	    -H "Authorization: Bearer $$OPENWEBUI_USER_API_KEY" \
	    | python3 -m json.tool --indent 2

projects-bootstrap: ## One-time admin enable of workspace.knowledge + sharing.public_knowledge so user keys can create + publicly share project-memory KBs (run after `make api-keys`)
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
	@docker exec -it kb-proxy sh

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
	@echo "Next: make provision  (.env is gone; compose needs it for every command)."

clean-backup: ## Remove the retention trees (./.gdrive-backup + ./.config-backup). Non-destructive: does not touch the stack, ./data, .env, or .env.local.
	@rm -rf ./.gdrive-backup ./.config-backup
	@echo "Removed ./.gdrive-backup + ./.config-backup (retention)."

backup: ## DR snapshot: if running, stop stack; tar resolved DATA_ROOT + .env + .env.local into ./.backups/knowledge-base_backup-<host>-<UTC>.tar (root read, host-owned, mode 0600); restart only if it was running. Restore is manual — see docs/operations.md "Disaster recovery".
	@set -euo pipefail; \
	test -f .env || { echo "MISSING .env — run: make bootstrap"; exit 1; }; \
	test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }; \
	DATA_ROOT="$$(grep -E '^DATA_ROOT=' .env 2>/dev/null | cut -d= -f2- || true)"; \
	DATA_ROOT="$${DATA_ROOT:-./data}"; \
	REALDATA="$$(readlink -f "$$DATA_ROOT")"; \
	test -d "$$REALDATA" || { echo "MISSING data dir ($$REALDATA from DATA_ROOT=$$DATA_ROOT) — nothing to back up"; exit 1; }; \
	WAS_RUNNING=0; \
	if [ -n "$$($(COMPOSE) ps -q 2>/dev/null)" ]; then WAS_RUNNING=1; fi; \
	mkdir -p .backups && chmod 700 .backups; \
	TS=$$(date -u +%Y%m%dT%H%M%SZ); HOST=$$(hostname -s); \
	TARBALL=".backups/knowledge-base_backup-$$HOST-$$TS.tar"; TMP="$$TARBALL.tmp"; \
	cleanup() { rc=$$?; rm -f "$$TMP"; \
	  if [ "$$WAS_RUNNING" = 1 ]; then echo "==> restarting stack"; $(MAKE) --no-print-directory start || true; fi; \
	  exit $$rc; }; \
	trap cleanup EXIT HUP INT TERM; \
	if [ "$$WAS_RUNNING" = 1 ]; then \
	  echo "==> stopping stack for a clean snapshot"; $(MAKE) --no-print-directory stop; \
	  if [ -n "$$($(COMPOSE) ps -q 2>/dev/null)" ]; then \
	    echo "FAIL: stack still running after stop — aborting before tar (no tarball written)"; exit 1; fi; \
	fi; \
	echo "==> tarring $$REALDATA + .env + .env.local -> $$TARBALL (root read; host-owned output)"; \
	umask 077; \
	docker run --rm \
	  -v "$$REALDATA:/staging/data:ro" \
	  -v "$(CURDIR)/.env:/staging/.env:ro" \
	  -v "$(CURDIR)/.env.local:/staging/.env.local:ro" \
	  alpine tar -cf - -C /staging --exclude=data/openwebui/check-exports \
	    data .env .env.local > "$$TMP"; \
	tar -tf "$$TMP" >/dev/null || { echo "FAIL: tarball validation (tar -tf) failed"; exit 1; }; \
	mv "$$TMP" "$$TARBALL"; \
	SHA=$$(sha256sum "$$TARBALL" | cut -d' ' -f1); SIZE=$$(du -h "$$TARBALL" | cut -f1); \
	echo "==> backup complete: $$TARBALL ($$SIZE)  sha256=$$SHA"; \
	echo "==> restore: see docs/operations.md \"Disaster recovery\""
