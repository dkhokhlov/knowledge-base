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
        health ci test test-unit test-live-RO test-iso test-iso-shared test-iso-single test-iso-long test-long test-output api-keys admin-signup config-rag \
        users-create users-list users-search \
        config-ocr \
        gdrive-meta \
        kb-index kb-index-finalize kb-bootstrap kb-status kb-sync kb-desc-backfill \
        kb-public-read kb-check kb-finalize kb-bm25-init kb-bm25-rollback kb-bm25-check \
        projects-bootstrap \
        clean clean-all clean-test clean-tests clean-backup backup

help: ## Show this help
	@awk 'BEGIN {FS=":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

provision: ## ONE-TIME from-scratch setup: bootstrap + pull-models + start + admin-signup + api-keys (auto OCR) + projects-bootstrap + config-rag + kb-bm25-init (ParadeDB pg_search BM25 index) + gdrive KB. Leaves the stack running. Each top-level subdir of ./root/ is one KB (named after the subdir); ./root/gdrive/ is the gdrive KB.
	@set -e; \
	  echo "==> 1/9 bootstrap (creates .env/.env.local + secrets + ./data dirs)"; make bootstrap; \
	  echo "==> 2/9 pull-models (BLOCKING: pulls base LLM + ctx variant + embedder + deepseek-ocr from Ollama)"; make pull-models; \
	  echo "==> 3/9 start (preflight + docker compose up -d; ocr sidecar via COMPOSE_PROFILES=ocr in .env)"; make start; \
	  echo "==> waiting for stack /health (OWUI has a 40s start period)..."; \
	  _KB_DOMAIN_OVR=$${KB_DOMAIN:-}; _OCR_ENABLED_OVR=$${OCR_ENABLED:-}; \
	  set -a; . ./.env; set +a; \
	  [ -n "$$_KB_DOMAIN_OVR" ] && export KB_DOMAIN="$$_KB_DOMAIN_OVR"; \
	  [ -n "$$_OCR_ENABLED_OVR" ] && export OCR_ENABLED="$$_OCR_ENABLED_OVR"; \
	  H=$${KB_HOST:?KB_HOST not set -- export KB_HOST=http://host:port (see .env.template)}; \
	  i=0; until curl -sf "$$H/health" >/dev/null 2>&1; do i=$$((i+1)); [ $$i -lt 60 ] \
	    || { echo "stack did not become healthy in 120s ($$H/health)" >&2; exit 1; }; sleep 2; done; \
	  echo "  stack healthy ($$H/health)"; \
	  echo "==> 4/9 admin-signup (creates the admin@<KB_DOMAIN> account)"; make admin-signup; \
	  echo "==> 5/9 api-keys (admin key; auto-configures OWUI -> markitdown-ocr when OCR_ENABLED=true)"; make api-keys; \
	  echo "==> 6/9 projects-bootstrap (one-time admin enable of workspace.knowledge + sharing.public_knowledge so user keys can create + publicly share project-memory KBs)"; make projects-bootstrap; \
	  echo "==> 7/9 config-rag (strict-grounding RAG template + rag.ollama.base_url sync)"; make config-rag; \
	  echo "==> 8/9 kb-bm25-init (ParadeDB pg_search extension + BM25 index on document_chunk; drops the dead GIN FTS index; needs the kb-postgres image)"; make kb-bm25-init; \
	  echo "==> 9/9 kb-bootstrap KB=gdrive (creates the gdrive KB + grants public read; name-based, no GDRIVE_KB_ID)"; make kb-bootstrap KB=gdrive; \
	  echo; echo "==> provision complete — stack is running."; \
	  echo "    Populate the gdrive KB (one-time):           make kb-sync && make kb-index KB=gdrive"; \
	  echo "    Add a new KB: drop a folder at ./root/<name>/ then: make kb-bootstrap KB=<name> && make kb-index KB=<name>"; \
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
	  $(COMPOSE) config | sed -E 's/(WEBUI_SECRET_KEY|OPENWEBUI_ADMIN_API_KEY|OPEN_WEBUI_API_KEY|OCR_SERVICE_TOKEN|OPENWEBUI_FIRST_PASSWORD): .*/\1: <redacted>/'

health: ## Probe the stack /health (Caddy -> api-gateway aggregated, reflects OWUI)
	@set -a; . ./.env; set +a; \
	H=$${KB_HOST:?KB_HOST not set -- export KB_HOST=http://host:port (see .env.template)}; \
	curl -sf "$$H/health" >/dev/null \
	  && echo "stack healthy ($$H/health)" || { echo "stack DOWN ($$H/health)"; exit 1; }

ci: ## Provision the .venv: uv sync (Python 3.12 from .python-version + locked deps from uv.lock). Idempotent; prereq for every test target.
	uv sync

test: test-unit test-live-RO ## Run the unit tests + live-stack read-only system tests (needs `make start` first). Iso + long tests run via `make test-iso` / `make test-long`.

test-unit: ci ## Run the python unit tests only (no stack needed; marker = unit).
	@$(PYTEST) -m unit -v

test-live-RO: ci ## Run the live-stack read-only system tests (test_01/02/03; needs `make start` first; marker = integration). test_09 is now iso long (no integration long test remains).
	@$(PYTEST) -m "integration" -v

test-iso-shared: ci ## Run the iso tests that share ONE session-provisioned clean-prod stack (test_04/05/06/07/10/11/13/14/15; marker = iso and shared). Auto-provisions via the iso_env session fixture; GPU/RAM: a 2nd stack on the shared Ollama.
	@$(PYTEST) -m "iso and shared" -v

test-iso-single: ci ## Run the iso tests that each get their own named clean-prod stack (test_12; marker = iso and not long and not shared). GPU/RAM: a 2nd stack on the shared Ollama.
	@$(PYTEST) -m "iso and not long and not shared" -v

test-iso: test-iso-shared test-iso-single ## Run all short iso tests (shared + single; excludes long). Each provisions a throwaway clean-prod stack.

test-iso-long: ci ## Run the long iso tests (test_08 agent-surface + test_09 at-scale gdrive; marker = iso and long). GPU/RAM heavy; runs many minutes.
	@$(PYTEST) -m "iso and long" -v

test-long: test-iso-long ## Run the long iso tests (same as test-iso-long).

test-output: ci ## Unit-test CLI JSON output schemas (no stack needed)
	@.venv/bin/python tests/test_output_json.py -v

clean-test: ## Tear down ONE isolated e2e run + remove its clone. NAME=<name> (default e2e) + optional STAMP=<stamp> (newest stamp under .test-env/ if unset). Pass NAME=<suffix> to match an iso run (test_09 uses gdrive; the iso_env_named fixture names its clone .test-env/<stamp>-<suffix>/). Safe anytime (no-op if absent). Delegates to scripts/lib-e2e-env.sh (shared with the conftest iso fixtures, tests/conftest.py).
	@bash -c '. scripts/lib-e2e-env.sh; e2e_down "$${NAME:-e2e}" "$${STAMP:-}"'

clean-tests: ## Manual hygiene flush: remove EVERY .test-env/<stamp>-<name>/ clone + stranded stamped e2e docker. Prints each clone's HEAD + unmerged commits before removing (a warning, not a hard refuse). NAME=<name> flushes only that name's clones. Run periodically -- stamped clones accumulate per e2e run (no autoclean). Delegates to scripts/lib-e2e-env.sh.
	@bash -c '. scripts/lib-e2e-env.sh; e2e_clean_tests "$${NAME:-}"'

api-keys: ## Provision the admin API key into .env.local (run after `make start` + admin signup)
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

config-rag: ## Set the strict-grounding RAG template in Open WebUI (run after `make api-keys`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local 	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/config-rag.sh

config-ocr: ## Re-assert OWUI CONTENT_EXTRACTION_ENGINE=external -> markitdown-ocr (auto-set by make api-keys when OCR_ENABLED=true; re-run after a DB reset)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@_OVR="$${OCR_ENABLED:-}"; set -a; . ./.env 2>/dev/null; set +a; \
	  if [ -n "$$_OVR" ]; then export OCR_ENABLED="$$_OVR"; fi; \
	  if [ "$${OCR_ENABLED:-true}" != "true" ]; then echo "OCR_ENABLED=$${OCR_ENABLED} — nothing to configure"; exit 0; fi; \
	  grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	    || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }; \
	  grep -qE '^OCR_SERVICE_TOKEN=.+$$' .env.local \
	    || { echo "MISSING OCR_SERVICE_TOKEN in .env.local (run: make bootstrap with OCR_ENABLED=true)"; exit 1; }; \
	  ./scripts/config-ocr.sh

gdrive-meta: ## Generate per-file .meta YAML sidecars (Drive description, [labels], file attributes, approval) next to each synced gdrive file under ./root/gdrive (read-only; reuses rclone token; never prints credentials). Set FILE=<id> for one file, DRIVE=<name> for one drive, DRY_RUN=1 to preview. .meta sidecars are excluded from indexing and protected from sync deletion (./root/.kb-ignore globals *.meta + *.json).
	@./scripts/gdrive-meta.py $${FILE:+--file "$$FILE"} $${DRIVE:+--drive "$$DRIVE"} $${DRY_RUN:+--dry-run}

kb-index: ## Reconcile ./root/<KB>/ trees into their OWUI KBs via api-gateway POST /index (admin; incremental). KB=<name> indexes one KB; KB=<glob> (e.g. xgen-*) expands against ./root/ and indexes every match; no KB= indexes EVERY top-level non-dot subdir of ./root/. Self-heals FAILED files (delete + re-upload) by default. Set RETRY_PENDING=1 to also retry stalled PENDING files, INDEX_ALL=1 for a full re-index. Per-KB client-side in-flight guard (refuses to dispatch if pending+processing>0; exempt RETRY_PENDING=1); the KB is resolved BY NAME (paginated, unique-or-fail). INDEX-ONLY — run `make kb-sync` first for gdrive (rclone into ./root/gdrive/).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@KB="$${KB:-}"; \
	  if [ -z "$$KB" ]; then \
	    ./scripts/kb-index.sh $${INDEX_ALL:+--index-all} $${RETRY_PENDING:+--retry-pending}; \
	  else \
	    case "$$KB" in \
	      *[*\?[]*) \
	        matched=0; \
	        for d in root/$$KB/; do \
	          [ -d "$$d" ] || continue; \
	          matched=1; \
	          ./scripts/kb-index.sh $${INDEX_ALL:+--index-all} $${RETRY_PENDING:+--retry-pending} --kb "$$(basename "$$d")" || exit 1; \
	        done; \
	        [ "$$matched" = "1" ] || { echo "FAIL  no ./root/ subdir matches glob '$$KB'" >&2; exit 1; } ;; \
	      *) \
	        ./scripts/kb-index.sh $${INDEX_ALL:+--index-all} $${RETRY_PENDING:+--retry-pending} --kb "$$KB" ;; \
	    esac; \
	  fi

kb-bootstrap: ## Create (or resolve) an OWUI KB named after a ./root/ subdir + grant public read (user:*). KB=<name> bootstraps one KB; no KB= bootstraps EVERY top-level non-dot subdir of ./root/. KB=<name> with --resolve (via the script) prints the kb_id only. Idempotent (re-run re-asserts the grant). Run after `make api-keys`. The gdrive KB is KB=gdrive.
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/kb-bootstrap.sh

kb-desc-backfill: ## One-time backfill of the source-attribute kv into existing KB descriptions (admin). Idempotent + non-destructive: SKIPs any KB whose description already carries a `source=` kv; appends the kv to the prose lead for legacy root/projects KBs; skips unparseable descriptions. access_grants is NOT clobbered (the user:* public-read grant survives). Run on the stack host.
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/kb-desc-backfill.sh

kb-public-read: ## Grant public read (user:*) on EVERY knowledge base + enable sharing.public_knowledge so all authenticated users read all KBs (admin). Backfills existing KBs; re-run as a safety net for KBs created outside the flows (e.g. via the OWUI UI).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/kb-public-read.sh

kb-check: ## Cross-DB health check (OWUI SQLite + pgvector vector store). Audit both stores, report 12 inconsistency classes, advise purge. PURGE=1 to purge the safe class (3 orphan file-{id} collections; BACKUP=1 default exports first). PURGE=1 MAINT=1 stops OWUI to purge maint classes ONLY (5b leaked KB vectors, 7 orphan junction, 8 dead-KB junction) -- the safe (class 3, OWUI live) and maint (OWUI stopped) tiers are mutually exclusive; run separately. REPAIR=1 stops OWUI to repair class-9 stuck-processing-while-linked files (linked + content + vectors, but status stuck at processing) -> completed; combine with PURGE/MAINT to do both. PRUNE_KB=1 deletes stale source=root KBs whose ./root/<name>/ dir is gone (class 11); always exports a timestamped backup first (mandatory); needs OWUI running (incompatible with MAINT=1/REPAIR=1); separate from PURGE (orphan-vector cleanup). KB=<id> scopes the KB-tagged classes; JSON=1 machine-readable; SHOW_NAMES=1 prints filenames (default ids-only).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  if [ "$${PRUNE_KB:-0}" = "1" ]; then \
	    if [ "$${MAINT:-0}" = "1" ] || [ "$${REPAIR:-0}" = "1" ]; then \
	      echo "FAIL  PRUNE_KB=1 needs OWUI running (incompatible with MAINT=1 / REPAIR=1)"; exit 1; \
	    fi; \
	  fi; \
	  ROOT_DIRS_ARG=; \
	  if ROOT_DIRS=$$(python3 -c 'import os,sys,json; r="root"; sys.exit(1) if not os.path.isdir(r) else None; print(json.dumps(sorted(d for d in os.listdir(r) if not d.startswith(".") and os.path.isdir(os.path.join(r,d))), separators=(",",":")))' 2>/dev/null); then \
	    ROOT_DIRS_ARG="--root-dirs $$ROOT_DIRS"; \
	  else \
	    if [ "$${PRUNE_KB:-0}" = "1" ]; then echo "FAIL  ./root not found / unreadable — refusing to prune without a root set (would prune ALL root KBs)"; exit 1; fi; \
	  fi; \
	  OWUI="$${OWUI_CONTAINER:-kb-openwebui}"; \
	  VENV="-e VECTOR_DB"; \
	  PG_ENV=; NET=; \
	  if [ "$${VECTOR_DB:-}" = "pgvector" ]; then \
	    PG_ENV="-e PGVECTOR_USER -e PGVECTOR_PASSWORD -e PGVECTOR_DB -e PGVECTOR_DB_URL"; \
	    NET="--network $${COMPOSE_PROJECT_NAME:-knowledgebase}_owui_net"; \
	  fi; \
	  if [ "$${MAINT:-0}" = "1" ] || [ "$${REPAIR:-0}" = "1" ]; then \
	    echo "==> maintenance window: stopping $$OWUI (direct vector/SQLite writes)"; \
	    docker stop $$OWUI >/dev/null; \
	    trap 'echo "==> restarting $$OWUI"; docker start $$OWUI >/dev/null' EXIT; \
	    docker run --rm --entrypoint /usr/local/bin/python3 $$NET \
	      -v "$$(readlink -f "$${DATA_ROOT:-./data}")/openwebui:/app/backend/data" \
	      -v "$(CURDIR)/scripts/kb_check.py:/app/kb_check.py:ro" \
	      $$VENV $$PG_ENV \
	      ghcr.io/dkhokhlov/open-webui:"$${OPENWEBUI_IMAGE_TAG:?OPENWEBUI_IMAGE_TAG required in .env}" \
	      /app/kb_check.py $${KB:+--kb $$KB} $${JSON:+--json} $${SHOW_NAMES:+--show-names} \
	        $${PURGE:+--purge} $${MAINT:+--maint} $${REPAIR:+--repair} $$ROOT_DIRS_ARG \
	        $$( [ "$${BACKUP:-1}" = "0" ] && echo --no-backup ); \
	  else \
	    KEY_ENV=; if [ "$${PURGE:-0}" = "1" ] || [ "$${PRUNE_KB:-0}" = "1" ]; then \
	      [ -n "$${OPENWEBUI_ADMIN_API_KEY:-}" ] || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (PURGE=1 / PRUNE_KB=1 needs it)"; exit 1; }; \
	      KEY_ENV="-e OPENWEBUI_ADMIN_API_KEY"; fi; \
	    docker exec -i $$KEY_ENV $$VENV $$PG_ENV $$OWUI python3 - < scripts/kb_check.py \
	      $${KB:+--kb $$KB} $${JSON:+--json} $${SHOW_NAMES:+--show-names} \
	      $${PURGE:+--purge} $$ROOT_DIRS_ARG \
	      $$( [ "$${PRUNE_KB:-0}" = "1" ] && echo --prune-kb ) \
	      $$( [ "$${BACKUP:-1}" = "0" ] && echo --no-backup ); \
	  fi

kb-bm25-check: ## Release gate for patch 10 + patch 11: probe the ParadeDB pg_search extension + the idx_document_chunk_bm25 index + the ||| / pdb.score ranking path + colon-safe + zero-token + the lexical-dsl @@@ parse_with_field phrase path + malformed-DSL-raises (lenient => false). Exit 0 green / 1 red. A red probe = do not ship (a broken/missing index silently degrades every query to the langchain full-collection fallback). Run after `make kb-bm25-init`. Uses psycopg2 in the OWUI image (pgvector env); no OWUI SQLite/REST needed.
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  OWUI="$${OWUI_CONTAINER:-kb-openwebui}"; \
	  if [ "$${VECTOR_DB:-}" != "pgvector" ]; then \
	    echo "FAIL  VECTOR_DB=$${VECTOR_DB:-<unset>}: kb-bm25-check needs VECTOR_DB=pgvector." >&2; exit 1; \
	  fi; \
	  PG_ENV="-e PGVECTOR_USER -e PGVECTOR_PASSWORD -e PGVECTOR_DB -e PGVECTOR_DB_URL"; \
	  docker exec -i -e VECTOR_DB $$PG_ENV $$OWUI python3 - < scripts/kb_check.py --bm25-gate

kb-status: ## Show index/sync status via api-gateway GET /status as a JSON ARRAY (one element per KB: dir, kb_id, source_count, indexed_count, pending/processing/failed, started_at, runtime) WITHOUT the per-file listings. KB=<name> selects one KB; no KB= shows EVERY top-level non-dot subdir of ./root/. FILES=1 keeps the indexed_files/pending_files/failed_files listings in each element. The KB is resolved BY NAME; no GDRIVE_KB_ID.
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@set -a; . ./.env; . ./.env.local 2>/dev/null || true; set +a; \
	  H=$${KB_HOST:?KB_HOST not set -- export KB_HOST=http://host:port (see .env.template)}; \
	  [ -n "$${KB_API_KEY:-}" ] || { echo "MISSING KB_API_KEY in the shell env (source ~/.api_keys, or run: make users-create EMAIL=...)"; exit 1; }; \
	  KB="$${KB:-}"; \
	  if [ -n "$$KB" ]; then \
	    _list=$$(mktemp); printf '%s\n' "$$KB" > "$$_list"; \
	  else \
	    _list=$$(mktemp); \
	    find root -maxdepth 1 -mindepth 1 -type d ! -name '.*' -printf '%f\n' 2>/dev/null | sort > "$$_list"; \
	    if [ ! -s "$$_list" ]; then \
	      rm -f "$$_list"; \
	      echo "FAIL  no top-level non-dot subdirs under ./root/ (run: make kb-bootstrap)" >&2; exit 1; \
	    fi; \
	  fi; \
	  _kid() { KB="$$1" ./scripts/kb-bootstrap.sh --resolve 2>/dev/null \
	    || { echo "FAIL  could not resolve KB '$$1' by name (run: make kb-bootstrap KB=$$1)" >&2; return 1; }; }; \
	  _out=$$(mktemp); rc=0; \
	  while IFS= read -r name; do \
	    [ -n "$$name" ] || continue; \
	    KID=$$(_kid "$$name") || { rc=1; continue; }; \
	    body=$$(curl -sS "$$H/status?kb_id=$$KID&dir=$$name&json=1" -H "Authorization: Bearer $$KB_API_KEY"); \
	    if [ -n "$${FILES:-}" ]; then \
	      printf '%s\n' "$$body" >> "$$_out"; \
	    else \
	      printf '%s' "$$body" | python3 -c 'import sys,json; o=json.load(sys.stdin); [o.pop(k) for k in ("indexed_files","pending_files","failed_files") if k in o]; print(json.dumps(o,ensure_ascii=False))' >> "$$_out"; \
	    fi; \
	  done < "$$_list"; \
	  rm -f "$$_list"; \
	  if [ $$rc -ne 0 ]; then rm -f "$$_out"; exit 1; fi; \
	  python3 -c 'import sys,json; print(json.dumps([json.loads(l) for l in sys.stdin if l.strip()], indent=2, ensure_ascii=False))' < "$$_out"; \
	  rm -f "$$_out"

kb-finalize: ## Finalize a drain: rebuild the pgvector ivfflat vector index so freshly-embedded vectors become queryable (pgvector is the only supported backend; fails loud on any other VECTOR_DB). KB=<name> selects the drain to wait on with --wait; no KB= waits on EVERY top-level non-dot subdir of ./root/. Run AFTER the drain is terminal (or use kb-index-finalize to dispatch + wait). REINDEX is INSTANCE-WIDE on the shared document_chunk table, so this first requires EVERY KB under ./root/ terminal + acquires a host lock (serializes concurrent finalizes). Logs the REINDEX duration. Named "finalize" not "reindex": on a fresh drain the vectors were never queryable (ivfflat folds post-build rows in only on REINDEX), and to avoid collision with the gateway reindex_all (POST /index?reindex_all=1 RE-PROCESSES files — a different op at a different layer).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@./scripts/kb-finalize.sh

kb-index-finalize: kb-index ## One-command pipeline: dispatch the KB async drain (make kb-index = POST /index), then wait for it to terminate (poll GET /status to pending+processing=0, timeout GDRIVE_TEST_WAIT default 2400s), then finalize (REINDEX ivfflat) so the freshly-embedded vectors become queryable. KB=<name> waits on one KB; no KB= waits on EVERY top-level non-dot subdir (matches make kb-index's all-KB dispatch). Fails loud if the drain does not terminate (do not REINDEX while inserts are in flight). REINDEX is instance-wide, so kb-finalize first requires EVERY KB under ./root/ terminal + acquires a host lock. pgvector only (fails loud on any other VECTOR_DB). For gdrive run `make kb-sync` (rclone) first, then this.
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@./scripts/kb-finalize.sh --wait

kb-bm25-init: ## Create the ParadeDB pg_search extension + the BM25 index on document_chunk (the FTS arm patch 10 queries) and drop the dead GIN FTS index. Idempotent. Needs the kb-postgres image (shared_preload_libraries=pg_search baked in); fails loud on the stock pgvector image. Container-targeted via POSTGRES_CONTAINER (iso) or kb-postgres (live). Run after `make start` (document_chunk must exist); no gdrive drain in flight (CREATE INDEX is a brief ACCESS EXCLUSIVE lock).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@./scripts/kb-bm25-init.sh

kb-bm25-rollback: ## Roll back patch 10: DROP the BM25 index + the pg_search extension (DROP INDEX before DROP EXTENSION — pg_search refuses to drop while the index depends on it). Refuses to run if pg_search is already absent. Run BEFORE reverting the kb-postgres + openwebui images to their stock/pre-patch-10 builds (dropping the index while the patch-10 OWUI image runs makes the FTS arm error -> langchain per-query full-collection fallback).
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@./scripts/kb-bm25-rollback.sh

kb-sync: ## Sync external sources into ./root/ (SYNC-ONLY; does NOT index). Today this is gdrive: rclone sync all shared drives into ./root/gdrive/ (delta; deleted/overwritten retained in ./.gdrive-backup). After this, run `make kb-index KB=gdrive` to reconcile the synced tree into the OWUI gdrive KB (sync and index are split so each is independently retryable). INDEX_ALL / RETRY_PENDING apply to `make kb-index`, not this stage. Fails fast on any transfer error.
	@./scripts/gdrive-sync

projects-bootstrap: ## One-time admin enable of workspace.knowledge + sharing.public_knowledge so user keys can create + publicly share project-memory KBs (run after `make api-keys`)
	@test -f .env.local || { echo "MISSING .env.local — run: make bootstrap"; exit 1; }
	@grep -qE '^OPENWEBUI_ADMIN_API_KEY=.+$$' .env.local \
	  || { echo "MISSING OPENWEBUI_ADMIN_API_KEY in .env.local (run: make api-keys)"; exit 1; }
	@./scripts/projects-index-bootstrap.sh

clean: ## Teardown: stop + remove containers + network. KEEPS ./data and .env.local.
	@$(COMPOSE) down --remove-orphans

clean-all: ## Full wipe: clean + DELETE ./data + ./.gdrive-backup + backup-and-remove .env + .env.local. Keeps graphiti/config.yaml, caddy/Caddyfile, and the ./root source mirror (./root/gdrive corpus + ./root/.kb-ignore).
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
	@echo "Wiped containers, ./data, ./.gdrive-backup, .env, .env.local (backed up to ./.config-backup/<TS>). graphiti/config.yaml, caddy/Caddyfile, ./root source mirror preserved."
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
