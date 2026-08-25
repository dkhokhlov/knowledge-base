# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Source file `mtime` in `retrieve` hits.** The kb-gateway captures each
  gdrive file's mtime (rclone-preserved filesystem mtime) as ISO-8601 UTC at
  upload and stores it in `File.meta.data.mtime`. A new Open WebUI custom-image
  patch (`open-webui/apply_mtime_to_chunks.py`; see `open-webui/PATCH.md`
  patch 3) injects `mtime` into the `metadata=` dict `process_file` passes to
  `save_docs_to_vector_db`, so it lands in every chunk (all three `process_file`
  paths, incl. the no-results fallback). `owui.py` `flatten_chroma` surfaces it
  in each hit. Requires the OWUI image rebuild + a re-OCR to populate existing
  chunks (new chunks get it on upload). Published as
  `ghcr.io/dkhokhlov/open-webui:0.11.0-pathdedup-idem-mtime`.
  `tests/test_gateway_unit.py` covers the gateway capture + upload multipart;
  `tests/test_output_json.py` covers the wrapper surfacing.
- **`retrieve-projects --account` wildcard globs** — the `--account` filter now
  accepts an fnmatch glob (e.g. `*@corp.com` / `*` for all visible KBs) in
  addition to an exact owner email. Backward-compatible: an exact email has no
  glob chars and matches literally. Doc updated in `SKILL.md` + `docs/operations.md`.

### Changed

- **`retrieve` hits carry page-round-trip metadata.** `owui.py` `flatten_chroma`
  now propagates `file_id`, `page`, `start_index`, and `source` from Chroma
  chunk metadata into each hit (previously dropped). Enables the page
  round-trip in `docs/ocr.md`: a hit's `file_id` + `page` -> `owui.py file
  <file_id>` (save raw) -> `pdftotext`/`pdftoppm -f <page>` for that page and
  its neighbors. Absent metadata defaults to `None` / `""`. Wrapper-side
  only; no re-index needed (the metadata was already in Chroma, just not
  surfaced). `tests/test_output_json.py` enriched to assert the new keys.

### Fixed

- **Strip `[Image OCR]`/`[End OCR]` region markers from OCR output.**
  `markitdown/oursvc.py` strips the upstream markitdown-ocr
  `PdfConverterWithOCR` region markers (markdown italics wrappers around
  OCR'd image regions) from the extracted markdown at extraction time. They
  were retrieval noise (the inner OCR text is preserved). Requires the
  `markitdown-ocr` image rebuild + a re-OCR to clean the existing chunks.
  Published as `ghcr.io/dkhokhlov/markitdown-ocr:0.1.1-ollama-native`.

## [v1.4.0] — 2026-08-24

### Added

- **`make provision` target** — one-shot from-scratch setup that chains
  `bootstrap` → `pull-models` (blocking on Ollama) → `start` → `admin-signup` →
  `api-keys` (auto-configures OWUI → `markitdown-ocr` when `OCR_ENABLED=true`)
  → `rag-config` → `gdrive-index-bootstrap`, with a `/health` wait between
  `start` and `admin-signup` (OWUI has a 40s healthcheck start period). Leaves
  the stack running. Replaces the manual 7-step sequence; re-run after
  `make clean-all`, use `make start` for everyday restarts.

- **`make test-output` + `tests/test_output_json.py`** — a stdlib `unittest`
  (no stack) that monkeypatches the scripts' HTTP layer and asserts every
  subcommand emits valid JSON with the expected schema (and, for the scripts,
  that it is compact). `make test` runs it first (with `|| status=1`), before
  the `tests/test_*.sh` integration suite.

- **`make users-create` / `users-list` / `users-search`** — operator make targets
  for Open WebUI KB user management (admin-only), via `scripts/users.sh`. `create`
  calls the kb-gateway `POST /admin/users` robust flow (create + signin + genkey +
  verify + rollback) and prints `{email, temp_password, kb_api_key, role, id}` as
  pretty JSON (indent 2); `list` (`GET /api/v1/users/all`) and `search`
  (`GET /api/v1/users/?query=<q>&page=1`, substring on name/email) print `{users,
  total}` as pretty JSON. Args via env: `EMAIL`/`NAME`/`ROLE` (create), `QUERY`
  (search). Replaces the in-skill admin command (see Changed).

### Changed

- **`search` → `retrieve` taxonomy split (breaking).** The `/kb` skill
  overloaded `search` across four meanings (KB-name lookup, KB document
  semantic fetch, cross-project semantic fetch, Graphiti facts search). Split
  into a 3-verb taxonomy: `search`/`search-kbs` = lexical KB-name lookup only
  (unchanged; upstream OWUI `/api/v1/knowledge/search`); `retrieve` = KB
  document semantic fetch (raw chunks, no LLM) — the default query path;
  `retrieve-projects` = cross-project semantic fetch; `retrieve` (kb-gateway) =
  Graphiti facts fetch; `rag` = retrieve + generate (`POST /memory/rag`),
  opt-in one-shot answer. **Breaking (the renamed subcommands shipped in
  v1.1.0):** `owui.py search <kb-id>`, `owui.py search-projects`, and
  `kb_gateway.py search` (facts) are renamed to `retrieve` / `retrieve-projects`
  / `retrieve`, and the gateway `POST /memory/search` route becomes
  `POST /memory/retrieve` (handler `_search` → `_retrieve`, OpenAPI spec
  updated). `search-kbs` (KB-name lookup) is unchanged. Client dispatch in
  `skills/claude/scripts/{owui.py,kb_gateway.py}` updated. Docs: `SKILL.md`
  rewritten to default the agent to `retrieve` over `rag`; "search" retained as
  a natural-language trigger synonym. Tests: test_02/06/08 updated to the
  `/memory/retrieve` endpoint and the `retrieve` subcommand.

- **Admin user provisioning moved out of the `/kb` skill to operator make
  targets.** The `kb_gateway.py user-create` subcommand (and its argparse entry,
  dispatch, the `test_user_create` unit test, and the test_08 e2e block) is
  removed — admin functions are done by the operator via `make users-create`
  (more transparent + controlled), matching the gdrive `/index` operator-only
  pattern. The gateway `POST /admin/users` endpoint is unchanged (still the
  provisioning backend; covered by `test_07`). The four `SKILL.md` copies drop
  the "KB user provisioning (admin)" section, the `create user` trigger, and the
  `user-create` reference; per-account key issuance now points to
  `make users-create`. **`KB_API_KEY` in `~/.api_keys` switched from the admin
  key (`OPENWEBUI_ADMIN_API_KEY`) to the read-scoped agent key
  (`OPENWEBUI_USER_API_KEY`)** — the skill is agent-scoped; the dropped
  `user-create` would 403 under the agent key anyway. Owner-scoped destructive
  ops (`forget`/`delete-edge`/`delete-episode`) stay allowed for the agent key.

- **Agentic-first JSON output.** The `/kb` skill scripts
  (`skills/claude/scripts/{owui.py,kb_gateway.py}`) and `make gdrive-status` now
  emit structured JSON, not human prose/glyphs — the consumer is an agent (an
  LLM), not a human. The scripts print **compact JSON** (single line; whitespace
  costs the agent tokens): every `cmd_*` success path returns a JSON object
  with a stable schema (`whoami`, `kbs`, `retrieve`, `index-projects`,
  `retrieve-projects`, `status-projects`, `groups`, `add`, `retrieve` → `facts`,
  `episodes`, `status`, `forget`, `delete-edge`/`delete-episode`).
  `rag` and `file` stay raw text (LLM answer / file content —
  wrapping is lossy); error/`sys.exit("FAIL ...")` paths stay prose on stderr.
  `make gdrive-status` emits **pretty JSON (indent=2)** (passes `?json=1`,
  pipes through `json.tool --indent 2`). The `--json` flag on `status-projects`
  is removed (output is JSON regardless).
  `index-projects` now returns `{projects:[{...,errors:[]}], total:{...},
  waited:[...]}`: per-file failures are collected into `errors` (not printed,
  which would break JSON), and a failed KB create is recorded as a project
  entry (`created:"failed"`) instead of dropped.

- **Config templates renamed + `KB_HOST` now shell-sourced.** `.env.example`
  → `.env.template` and `.env.local.example` → `.env.local.template` (the
  tracked templates `make bootstrap` copies from). `KB_HOST` is now commented
  out in `.env.template`, joining `OLLAMA_HOST`: both are deployment-specific,
  set in the shell env (`export KB_HOST=http://<host>:3000`,
  `export OLLAMA_HOST=http://<ollama-host>:11434`) so the shell value is not
  clobbered when the scripts source `.env` (shell env overrides `.env`). The
  scripts default to `http://localhost:3000` when `KB_HOST` is unset.
  **Migration:** rename an existing `.env.example`/`.env.local.example` to
  `.env.template`/`.env.local.template` (or delete them and let `make
  bootstrap` recreate from the new names); if you set `KB_HOST` in `.env`,
  export it in your shell env instead (uncomment in `.env` only as a fallback).
- **Configurable `KB_DOMAIN` + first-user credential rename (BREAKING for
  existing `.env.local`).** A new `KB_DOMAIN` var in `.env` (default
  `local.test`) drives the email domain of provisioned accounts: the first
  (admin) user is `admin@<KB_DOMAIN>` and the agent user is
  `agent@<KB_DOMAIN>` (was hardcoded `agent@local.test`). `make bootstrap`
  now writes `OPENWEBUI_FIRST_USER=admin@<KB_DOMAIN>` + a generated
  `OPENWEBUI_FIRST_PASSWORD` into `.env.local` (was: operator hand-filled). The
  first-user display name is `admin` (was `Admin`). A `make <target>
  KB_DOMAIN=<domain>` command-line override wins over `.env` for that run (per
  `bootstrap`, `api-keys`, and `test`). **Rename:**
  `OPENWEBUI_TEST_USER` → `OPENWEBUI_FIRST_USER`,
  `OPENWEBUI_TEST_PASSWORD` → `OPENWEBUI_FIRST_PASSWORD` across `bootstrap.sh`,
  `admin-signup.sh`, `api-keys.sh`, `e2e-restore-creds.sh`, `test-e2e.sh`,
  `test_03`/`test_04`, `.env.local.template`, the Makefile (redaction list +
  `admin-signup`/`api-keys` guards), and docs. `test_08` e2e users now use
  `@${KB_DOMAIN}`. **Migration:** an existing `.env.local` with the old
  `OPENWEBUI_TEST_USER=...` names is stale — run `make clean-all && make
  bootstrap` (re-provisions `admin@<domain>` + a fresh password) then `make
  admin-signup && make api-keys`. Changing `KB_DOMAIN` later likewise requires
  `clean-all && bootstrap` to recompute the first-user email (or edit
  `.env.local` by hand). The kb skill is unaffected (auth via `KB_API_KEY`, not
  user/password); only illustrative `agent@local.test` prose was generalized.
- **OCR is now a config flag (`OCR_ENABLED`), not a runtime toggle (BREAKING for
  existing stacks).** The `MARKITDOWN_OCR_PROVISIONED` runtime marker +
  `make ocr-bootstrap` + `make ocr-disable` are removed. Replaced by an
  `OCR_ENABLED` var in `.env` (ships in `.env.template`, default `true`, decided
  BEFORE `make bootstrap` — it cannot be toggled after; a change needs
  `make clean-all && make bootstrap`; overridable per run with
  `make <target> OCR_ENABLED=false`). When enabled, OCR is a first-class prereq
  provisioned by the standard chain — no separate step: `make bootstrap`
  generates `OCR_SERVICE_TOKEN` into `.env.local` (secret); `make pull-models`
  pulls `deepseek-ocr`; `make preflight` HARD-FAILs on a missing `deepseek-ocr`;
  `make start` adds `--profile ocr` (builds + starts the sidecar); `make
  api-keys` auto-sets the OWUI external-extraction routing
  (`CONTENT_EXTRACTION_ENGINE=external` +
  `EXTERNAL_DOCUMENT_LOADER_URL=http://markitdown-ocr:8080` + the API key, via
  `ocr-config.sh`). `make ocr-config` is retained to re-assert the OWUI DB keys
  (e.g. after a DB reset) and no-ops (exit 0) when `OCR_ENABLED!=true`.
  `clean-all` no longer drops a marker (`OCR_ENABLED` is preserved `.env`
  config). An unset `OCR_ENABLED` resolves to `true` (normalization), so an
  existing `.env` without the key behaves as enabled. **Migration / breaking:**
  an existing stack without `OCR_ENABLED` defaults to enabled → `make preflight`
  / `make start` hard-fail until `deepseek-ocr` is pulled (`make pull-models`)
  or `OCR_ENABLED=false` is set in `.env` (then `make clean-all && make
  bootstrap` to drop the token).
- **`HOST_UID` / `HOST_GID` moved from `.env` to `.env.local`** (per-machine
  values, not machine-independent config). `make bootstrap` now generates them
  into `.env.local` from the current user's `id -u` / `id -g` (kept if already
  set — override there if the gdrive owner uid:gid differs; `make clean-all`
  wipes `.env.local` and the next bootstrap re-derives them). Removed from
  `.env.template`. `compose.yml` interpolates them from the shell env (the
  container-creating targets source `.env.local`); absent → default 1000:1000.

- **`kb_gateway.py user-create` gains a `--json` flag.** The default output is
  human-readable `key: value` lines (`email`, `temp_password`, `kb_api_key`,
  `role`, `id`); pass `--json` to emit the raw JSON response instead, so a script
  can `json.load` stdout and extract `kb_api_key` / `temp_password` without
  parsing prose. `--email` + `--name` remain required. SKILL.md documents both.
- **SKILL.md notes that `add` is asynchronous.** Graphiti extracts entity edges
  in a background Ollama pass after `add` returns, so an immediate `retrieve` for
  the just-added fact can return `[]`; wait or retry before treating a 0-hit
  retrieve as "not remembered". Observed latency on this deployment (kb host → GPU Ollama host, `qwen2.5:14b`): ~10-15s warm, cold start can exceed 90s (varies by
  host/model). Documentation only — no code change.

- **`make test` is fast and deterministic; the full real-gdrive drain moved to
  `make test-e2e` only.** `tests/test_09_gdrive_index.sh` (the full real-gdrive
  `/index` + drain audit — slow, coupled to the live rclone-synced corpus) is
  no longer in the `make test` glob (the Makefile skips it with a notice); it
  runs under `make test-e2e`, which invokes it by path after the fresh
  `gdrive-sync`. New `tests/test_11_gdrive_index_fixture.sh` indexes a small,
  committed fixture set under `gdrive/.tests/` into a throwaway temp KB via
  `POST /index?path=.tests`, polls `GET /status?path=.tests` to terminal,
  audits failures (hard-fails on a "Failed to link" double-link race), and runs
  a deterministic marker-token semantic search. Self-contained: creates +
  grants `*` read + deletes the temp KB on EXIT (its files too, enumerated via
  `/api/v1/files/?content=false` filtered by `knowledge_id` so uploaded-but-
  unlinked orphans are caught); the committed fixture files are NOT deleted.
  Fixtures are small text files (`.txt`/`.md`/`.json`) plus minimal binary files
  (`.pdf`/`.docx`/`.pptx`) so the binary extraction path is exercised; when
  markitdown-ocr is not provisioned the binary fixtures fail extraction and
  surface as a genuine-failure notice (the text fixtures + marker search carry
  the test). `.gitignore` gains `!/gdrive/.tests/` + `!/gdrive/.tests/**` so the
  fixtures are tracked despite `/gdrive/*`. No compose/mount change, and no
  test-only env-var knob (`GDRIVE_FULL_DRAIN`) is introduced.
- **`/index` + `/status` gain a `path` query param; `walk_source` skips
  dot-names; `path` is exposed as a `gdrive-sync`/`gdrive-index`/`gdrive-status`
  parameter.** `POST /index?path=<relpath>` and `GET /status?path=<relpath>`
  target a subpath of the gdrive root (a directory or a single file; relative,
  normalized — absolute and `..` rejected). `path` is a SOURCE FILTER only: it
  scopes the `walk_source` manifest to the subpath. The reconcile is a FULL
  reconcile of that manifest — `sync/diff` `deleted` + `rmdir` flow through
  unscoped, so files removed from the source under that subpath ARE removed from
  the KB. Use a KB whose whole scope is that `path` (a dedicated/subpath KB,
  e.g. a throwaway test KB): on a SHARED KB `path` would delete every KB file
  outside the subpath. Entry `path` keys stay relative to the original root, so
  a subpath index and a later full `/index` see `unmodified` (not re-`added`).
  `path` on `/status` scopes `source_count` to the subpath; the file-status
  counts are KB-wide (`list_file_status` is KB-scoped by `knowledge_id`
  already), accurate when the KB's whole scope is `path`. The operator surface
  is `make gdrive-sync SCOPE_PATH=<relpath>` / `make gdrive-index SCOPE_PATH=<relpath>` /
  `make gdrive-status SCOPE_PATH=<relpath>` (the env var is `SCOPE_PATH`, not `PATH`,
  so it does not clobber the shell's executable-search `PATH`; the REST `?path=` is an internal detail).
  `walk_source` now skips dot-dirs and dot-files (replacing the hardcoded
  `{".sync-reports", ".sync.lock"}` set) — a full walk no longer indexes hidden
  files, and `path` opts into a dot-subtree (e.g. `.tests`).
  Production behavior change: verified `./gdrive` has 0 dot-named allowlisted
  files today; a future dot-named corpus doc would be skipped (caught by
  review).

- **`/index` no longer links files; `/status` reports real per-file progress.**
  The gateway stopped calling `files/batch/add` (link): `POST /files/` with
  `metadata.knowledge_id` queues OWUI's per-upload background task that runs the
  full pipeline — extract (markitdown-ocr) → embed into the KB collection → link
  — so that task is the sole linker (vectors are written before the link, making
  the link a valid completion proxy). `batch_add` linked before extraction,
  which raced the background task's own auto-link
  (`add_file_to_knowledge_by_id` has no exists-check → `IntegrityError` →
  swallowed → `_process_handler` raised "Failed to link file …" →
  `data.status='failed'`) on ~14/159 successfully-extracted files (false
  failures; their vectors were present + searchable) AND made `/status` report
  "done" while the GPU drain was still running. `/status` now reads
  `file.data.status` paged via `GET /api/v1/files/?content=false` (the
  `/knowledge/{id}/files` list defers `File.data`, so status read null there):
  `indexed_count` = `completed` (extracted + embedded + linked, searchable),
  `pending` = in extraction (OCR/GPU) or queued, `processing` = embedding +
  linking, `failed` = `{filename, error}`. Drain is terminal when
  `pending+processing=0` AND `completed+failed` covers `source_count`. Removed
  `kb_file_count` + `pending_files` helpers (the `file_count`/`/files/pending`
  reads they drove were the premature-done root cause). `/index` now re-triggers
  `failed` files every run (delete + re-upload so a fresh background task is
  queued — the upload-idempotency patch returns an existing same-hash file
  WITHOUT re-queueing, so the delete first is required); `?retry_pending=1`
  (off by default — interrupts in-flight OCR) also re-triggers `pending`.
  Response gains `retried`. `list_kb_files` passes `?limit=1000` (admin override
  of the 30-item default) so `reindex_all`'s drain + `dry_run` see every file.
  Fixed an `errors`-before-init `NameError` (a `create_directory` failure inside
  the mkdir loop hit `errors.append` before `errors = []` → 500). `tests/test_09`
  gates on the real drain (`pending+processing=0` AND `completed+failed>=source`,
  not the old false-green `pending=0`), fails on any "Failed to link" regression,
  surfaces genuine failures as a notice, and runs a deterministic semantic
  search (first completed file's stem → require ≥1 hit). `gdrive-sync` gains
  `--retry-pending` (→ `?retry_pending=1`) + logs `retried`. `docs/operations.md`
  updated for the new flow + `/status` vocabulary.
- **Replaced the `oikb` gdrive-indexer sidecar with a stateless indexer in
  kb-gateway.** The opaque `oikb` daemon (failed with 4/159 files indexed, no
  errors surfaced) is removed. gdrive indexing is now `POST /index` + `GET
  /status` endpoints in `gateway/app.py` (+ `gateway/owui.py` sync-protocol
  client: `sync/diff`, `upload_file` multipart, `files/batch/add`,
  `retrieval/process/files/batch`, `sync/cleanup`). The gateway walks the
  read-only `./gdrive` mount, drives OWUI's native sync protocol with its held
  `OPENWEBUI_ADMIN_API_KEY` (the caller's `KB_API_KEY` is authorization only),
  and returns per-file `{filename, status, error}` results — the diagnosis
  surface the daemon lacked. Stateless: no `history.db`, no `./data/oikb`, no
  manifest file; the KB is the state. Indexing is manual/on-demand only
  (`make gdrive-sync` chains rclone → `POST /index`; `make gdrive-index` runs
  `/index` alone; `INDEX_ALL=1` / `--index-all` force a full re-index). Empty
  source → 422 unless `?force=1` (no mass-delete). `make gdrive-status` is now
  `GET /status` (source vs indexed, pending, `✓ COMPLETE` / `○ remaining=N`;
  no ETA — no daemon). Removed: `.oikb.yaml`, `scripts/e2e-wait-indexer.sh`,
  `scripts/gdrive-status.sh`, the `gdrive-indexer` compose service + profile,
  `data/oikb/`. `compose.yml` adds `./gdrive:/gdrive:ro` +
  `OPENWEBUI_ADMIN_API_KEY` + `GDRIVE_KB_ID` + `KB_MAX_SIZE` + `user:
  ${HOST_UID:-1000}:${HOST_GID:-1000}` to kb-gateway. `.env.example` drops
  `OIKB_*` vars, adds `HOST_UID`/`HOST_GID`/`KB_MAX_SIZE` (rename
  `OIKB_MAX_SIZE` → `KB_MAX_SIZE` in your `.env`). `tests/test_09` rewrites to
  `POST /index` + poll `GET /status`. The OWUI path-aware-dedup +
  upload-idempotency patches STAY (the gateway depends on them). Greenfield:
  no backward-compat; the KB may be drained + re-indexed freely.
- **`/index` hardening (review + verification fixes).** `dry_run=1` now returns
  the plan before any mutation (previously `dry_run=1&reindex_all=1` drained the
  KB while reporting a no-op). New source subdirs are created via
  `POST /dirs/create` before their files upload (`sync/diff`'s `directory_map`
  only covers existing paths; without this, new-subdir files landed at KB root).
  Modified-entry orphans use `stale_file_id` (the field OWUI returns) and are
  merged into the final `sync/cleanup`. `walk_source` fails closed on
  `OSError` (a dropped file would reappear as `deleted` → cleanup → data loss).
  `upload_file` escapes the Content-Disposition filename. The unused `{}` POST
  body is drained so HTTP/1.1 keep-alive (Caddy's reused upstream) stays
  aligned — without it every other `/index` call 501'd with
  `Unsupported method ('{}POST')`. `/index` no longer calls
  `retrieval/process/files/batch`: that endpoint reads `file.data.content`
  directly and does NOT extract, so calling it right after upload ran BEFORE
  OWUI's per-upload background task populated content → every file reported
  "content is empty" and no vectors were written by the call (the spurious
  errors were also double-counted from both `results` and `errors`). OWUI's
  upload handler (POST /files/ with `metadata.knowledge_id`) already queues the
  full per-file pipeline — extract (markitdown-ocr) → embed into the KB
  collection (`process_file(collection_name=knowledge_id)`) → link — so the
  gateway now stops at `files/batch/add` (link) and lets the background task
  embed; `/status` polls `file.data.status` (+`error`, now surfaced per file)
  until pending drains to 0. `KB_INDEX_CHUNK` is removed (no chunked embed
  step); `OWUI_INDEX_TIMEOUT` (default 300s) stays for the sync-protocol calls
  (sync/diff, dirs/create, batch/add, sync/cleanup, list files). The catch-all
  logs the traceback (transparency). `gdrive-index-bootstrap` force-recreates
  kb-gateway after writing `GDRIVE_KB_ID` so the gateway receives the admin
  key + KB id.
- **`make` targets renamed to standard convention.** `clear` → `clean`
  (teardown; keeps `./data` + `.env.local`), `clear-all` → `clean-all` (full
  wipe), and the misused `clean` (rclone `./.gdrive-backup` retention tree) →
  `clean-backup`. Callers (`test-e2e.sh`, `e2e-restore-creds.sh`,
  `ocr-disable.sh`) + docs updated.
- **`gdrive-sync` now delta-syncs with backup retention.** Switched from
  `rclone copy` (additive, never deleted) to `rclone sync --backup-dir
  --delete-after`: files removed from Drive are deleted from `./gdrive` (and
  the next `/index` drops them from the KB via `sync/cleanup`), and
  deleted/overwritten files are moved into a dated
  `./.gdrive-backup/<UTC-ISO>/` dir OUTSIDE `./gdrive` (so `/index` does not
  index them) as a recovery net. `sync` deletes to match the source, so a
  bad/empty Drive mount empties `./gdrive` — but the removed files are
  recoverable from `./.gdrive-backup/`, not gone. The sync report gains a
  `backup-dir` header and a "Files backed up (deleted/overwritten)" line.
  `./.gdrive-backup/` is gitignored; new `make clean-backup` clears it (so does
  `make clean-all`).
- **`gdrive-sync` fail-fast + empty-source guard + non-downloadable exclude
  list.** Any transfer error now aborts the run immediately (the report is still
  written with the failing files + rclone reasons) instead of continuing to the
  next drive. A per-drive hard guard lists the remote drive before its sync and
  refuses to sync if it has 0 remote files (an empty remote — bad mount, revoked
  access, transient API return — would mass-delete the local drive dir, since
  `sync` deletes to match the source). Permanently non-downloadable files
  (Drive-admin download-forbidden `403 cannotDownloadFile` / `forbidden to
  download`, and dangling shortcuts) are listed in a new gitignored
  `./gdrive-exclude.conf` (`<drive_id>\t<drive-relative-path>` rows, drive-id
  keyed so per-drive `--exclude-from` scoping does not mis-exclude same-named
  files in other drives); `gdrive-sync` extracts the current drive's rows at
  runtime and passes them to rclone `--exclude-from`, so the rest of each drive
  downloads cleanly (exit 0). Append a row when a sync fails fast on a new
  non-downloadable file; transient errors (network/5xx) are NOT excluded (those
  fail fast by design). The exclude file holds Drive file paths
  (business-sensitive) and is deliberately NOT committed. Also fixed a log
  parser bug: per-file failures are now split on the fixed rclone prefix
  `": Failed to copy: "` instead of the first `": "` (filenames can contain
  `": "`, e.g. `"Report: Q4 review - …"`, which truncated the path).
- **`gdrive-sync` sets owner-only permissions.** After each sync, `./gdrive`
  (and `./.gdrive-backup/`) are normalized to files `600` / dirs `700` (Drive
  content is business-sensitive; rclone v1.60 has no `--umask`). kb-gateway
  reads `./gdrive` read-only as the host owner uid (`HOST_UID`, default 1000),
  so owner-only is still readable by the container.
- **`gdrive-sync` exclude file switched to an INI format; report gains
  COPY/UPDATE/DELETE + Files-excluded sections.** `./gdrive-exclude.conf`
  (gitignored — Drive paths are PII) is now an INI file: `[<drive name>]`
  sections hold patterns scoped to that shared drive (matched by name; the
  wrapper resolves name -> id at runtime from `rclone backend drives`), and a
  `[*]` section holds all-drives patterns. Patterns are passed VERBATIM to
  rclone `--exclude-from` (rclone-native: no `/` matches the basename at any
  depth, `/` matches the full path), so a global pattern under `[*]` (e.g.
  `*.tmp`) excludes that type from every drive and a lone `*` under a drive
  excludes that whole
  drive. The wrapper converts the INI file to `--exclude-from` per drive on the
  fly (the file is no longer in the old `<drive_id>\t<path>` tab format). The
  tracked `gdrive-exclude.conf.example` documents the format (the data file
  stays gitignored — no PII in the repo). The per-run
  `.sync-reports/sync-<UTC-ISO>.report` now lists every copied (`COPY`),
  updated (`UPDATE`), and deleted (`DELETE`) file (one per line, prefixed with
  its drive name), a "Files excluded" section (Drive files that matched the
  exclude patterns, parsed from rclone's `DEBUG : …: Excluded` lines — the run
  now logs at `--log-level DEBUG` for this), and a "Files not downloaded"
  section (failing paths + rclone reasons). Filenames containing `": "` (e.g.
  `"Report: Q4 review - …"`) are split on the fixed rclone message,
  not the first `": "`, so the path is not truncated.
- **`gdrive-sync` report gains a `dups` column + "Duplicates ignored" section;
  COPY/UPDATE/DELETE now correct under `--backup-dir`; name-collision guard.**
  The per-drive table adds a `dups` column and the report a "Duplicates ignored"
  section: when Drive holds two files at the same path (Drive permits duplicate
  names), rclone syncs one and ignores the rest (`Duplicate object found in
  source - ignoring`). `remote` counts every object, `local` only the one rclone
  keeps, so `dups` makes `remote − excluded − dups = local` reconcile (was an
  unexplained 1-file gap). The COPY/UPDATE/DELETE classifier now handles the
  `--backup-dir` verbs: an overwrite logs `Moved` (old to backup) + `Copied
  (new)`, classified `UPDATE`; a delete logs `Moved` + `Moved into backup dir`,
  classified `DELETE` — previously both were miscounted (overwrites as `COPY`,
  deletes invisible), because the parser only recognized the no-backup-dir verbs
  `Copied (replaced existing)` / `Deleted`. A name-collision guard aborts before
  sync if two drives map to the same local dir name (same Drive name or a
  sanitization collision like `A:B` -> `A_B`), preventing the second sync from
  deleting the first drive's files in the shared dir.
- **`gdrive-sync` takes a concurrency lock.** A hidden
  `<destination>/.sync.lock` (holder PID) prevents two runs from racing on the
  same destination — a second `rclone sync` would delete the first run's
  in-flight files (sync deletes to match source). A stale lock whose PID is no
  longer alive (crashed/killed holder) is retaken (`kill -0` liveness probe);
  the run aborts with exit 1 if another live run holds the lock. Release is on
  every exit path (the EXIT trap), gated by a `lock_held` flag so a contention
  abort does not delete the other holder's lock. PID reuse is a residual.

### Added

- **Projects memory indexing (Claude project memory → OWUI KBs).** New
  `index-projects` / `retrieve-projects` / `status-projects` subcommands in
  `skills/claude/scripts/owui.py` index `~/.claude/projects/<encoded>/memory/*.md`
  into OWUI KBs — one KB per project — so an agent recalls knowledge across
  Claude Code projects and sessions. The skill-side wrapper walks the host
  filesystem and calls OWUI REST directly with the caller's user key, which
  creates + owns each project KB (`KB.user.email == caller`); the kb-gateway is
  not involved. KB name = `<host>--<encoded-dir-without-leading-dash>`; per-file
  metadata carries `host`, `project`, `project_path`, `repo` (git repo name),
  `account`, `source_relpath` (and `repo` rides in the KB `description`).
  Every run is a full snapshot (always re-uploads; OWUI idempotency reuses
  unchanged files); a modified file is delete-then-uploaded (router `DELETE`
  cleans vectors — the upload's own reclaim does not); orphans are deleted.
  `retrieve-projects` filters by `--host`/`--project`/`--account`/`--kb-glob` and
  makes one retrieval call per KB (hit metadata carries no `knowledge_id`, so
  one-call-per-KB is the reliable attribution). `status-projects` walks up
  `realpath(cwd)` to match the project KB. Naming: "projects memory" (this) vs
  "facts memory" (the Graphiti knowledge graph, `kb-gateway` `/memory/*`) —
  "memory" is overloaded, so the two are named explicitly. One-time setup:
  `make projects-bootstrap` (admin) enables `workspace.knowledge` (off by
  default) so the user key can create KBs.

- **Custom Open WebUI overlay image (path-aware dedup hash + upload
  idempotency).** New `open-webui/` dir builds a thin overlay on the pinned
  official OWUI 0.11.0 image (digest-pinned, not the rolling `:main` tag) that
  applies two build-time backend patches via fail-loud apply scripts
  (`apply_path_hash.py`, `apply_upload_idempotency.py`; anchors asserted
  exactly-once, exit 1 on drift). See `open-webui/PATCH.md` for the why, the
  anchors, and the rebase steps. `compose.yml` points the `openwebui` service at
  `ghcr.io/dkhokhlov/open-webui:0.11.0-pathdedup-idem` (`pull_policy: never`;
  built locally, not pushed). Revert to stock by setting
  `OPENWEBUI_IMAGE_TAG=main` and removing the `build:` block; to run the dedup
  patch only, set `0.11.0-pathdedup`. Build seconds, not minutes (no frontend
  rebuild; two backend router files patched in place).
- `gdrive-sync` now writes `./gdrive/.sync-reports/sync-<UTC-ISO>.report` with
  the transfer summary and a "Files not downloaded" section (e.g. admin-
  protected / download-restricted Drive files, surfaced with their 403 reason).
- `gdrive/` directory tracked via `.gitkeep` (contents still gitignored).
- `tests/test_09_gdrive_index.sh` — tolerant indexer + RAG indexing check
  (passes on `status=ok` OR `partial`; skips clean when unprovisioned).
- `docs/operations.md` — new "KB RAG indexing (gdrive → Open WebUI)" section
  (provisioning, populating, monitoring, file types/skips, Chroma fd limit,
  persistent state) + gdrive env-var, make-target, and troubleshooting rows.

### Fixes

- **kb skill `file` command no longer crashes on binary files (PDF/DOCX/PPTX/
  XLSX/images).** The `/api/v1/files/{id}/content` endpoint returns the RAW file
  for binary formats, not extracted text, so `file <pdf-id>` raised
  `UnicodeDecodeError` (the shared `call()` did `r.read().decode()`). `cmd_file`
  now fetches directly, prints text files unchanged, and on a binary body saves
  the raw bytes to a temp file (extension inferred from `Content-Type`) with a
  note pointing at `pdftotext` / `retrieve <kb>`. Only `cmd_file` changed;
  `call()`/`jget()` (JSON-only) are untouched. Also redeployed the wrapper to
  `~/.claude/skills/kb/scripts/owui.py` (it was stale at the old
  `OPENWEBUI_BASE_URL` env name; the source had moved to `KB_HOST`), so the
  wrapper now resolves `KB_HOST` from `.env`/the shell with no `--base-url`.
- **oikb gdrive-indexer churn — unbounded disk growth (the "2b" bug).** oikb
  re-uploads files every `GDRIVE_INDEX_INTERVAL` cycle, and OWUI's upload
  handler minted a new uuid + on-disk blob + `FileModel` row on every POST with
  no lookup for an existing file at the same `(knowledge_id, directory_id,
  filename)`. Two failure modes piled up new storage items every cycle and never
  cleaned them up, so disk grew without bound:
  (a) **`DUPLICATE_CONTENT`** — OWUI dedups a KB by the SHA-256 of the extracted
  text only, so two source files with the same content at different paths
  collided; the second was rejected and never linked as a member. oikb has no
  per-file skip memory, so it re-uploaded the rejected file every cycle.
  (b) **`EMPTY_CONTENT`** — files that fail extraction (genuinely text-empty:
  image-only PDFs/slides, empty office docs) never become KB members, so
  `sync/diff` (which builds its known-set from members only) always reported
  them `added`, oikb re-uploaded them every cycle, and `sync/cleanup` (which
  runs only for `modified`/`deleted` members) never reached them. The dedup
  hash is computed after the `EMPTY_CONTENT` check, so the dedup fix alone
  could not help. Fixed with two build-time patches in the custom OWUI overlay
  image:
  - **Path-aware dedup hash** (`retrieval.py`, 2 sites): the dedup hash now
  includes the KB directory UUID + filename, so same-content-different-path
  files get different hashes and both index. Same path + name + content stays
  idempotent (same hash -> no re-process). When there is no `directory_id`
  (non-KB uploads, STT, `/file/add`), the hash is filename-aware — a safe
  improvement, no caller breaks.
  - **Upload idempotency** (`files.py`, 1 site): before minting a new uuid,
  the handler looks for an existing `FileModel` matching the same logical
  identity `(meta.data.knowledge_id, meta.data.directory_id, filename)`. Same
  identity + same byte hash -> reuse the existing `FileModel` + blob (no new
  storage, no re-extract); same identity + different hash (a failed file later
  edited to have content) -> reclaim the stale orphan and fall through to a
  normal new upload (self-heal); no match -> normal new upload. Guarded to oikb
  KB uploads only (metadata carries both `knowledge_id` and `file_hash`); every
  other caller is unchanged. Identity is path-based, not byte-hash, so it is
  orthogonal to the dedup patch (same-content-different-path files keep
  distinct identities and still upload as separate members).
  Verified on a clean state (`make test-e2e`): disk stays flat across sync
  cycles (one storage item per file, no per-cycle pile-up), the failed files
  are reused (logged `upload-idempotency reuse`), and same-content-different-
  path files both link as members. oikb still re-POSTs the non-member files
  each cycle, but each re-POST is now a cheap idempotent return (no new blob,
  no re-extract) — disk does not grow. The `oikb-side` alternative (skip
  already-attempted files in oikb) was rejected: oikb does not generate the
  file ids (OWUI does), and oikb's `history` is per-sync not per-file, so a
  skip fix needs new per-file tracking + a custom oikb image — more invasive
  than the one-file OWUI upload fix. Residual: failed-to-index files stay
  unlinked (they are genuinely text-empty and will not extract without OCR);
  if an extraction engine / OCR is enabled later, delete those orphans once
  and the next cycle re-uploads + extracts them. Deferred follow-up (not
  applied): patch `sync_knowledge_diff` to index orphan `FileModel`s by
  `meta.data.knowledge_id` so failed files are reported `unchanged` and oikb
  stops re-POSTing them entirely (eliminates the cheap no-op cycle); same
  JSON-on-SQLite scaling concern as the idempotency lookup — acceptable for
  this single-KB overlay, not for a large multi-tenant instance.
- **oikb retries a 0-byte source file every cycle.** OWUI `POST /api/v1/files/`
  rejects a 0-byte upload with `400`, and oikb has no min-size filter (only
  include/exclude globs + max-size), so a 0-byte source file was re-attempted
  every cycle. `.oikb.yaml` now excludes the generic 0-byte test-runner
  basename `*run_tests.py` (no PII; the `*` spans `/` in fnmatch so it matches
  at any depth).
- **oikb `history.db` lost on container recreation.** With the default
  `OIKB_CONFIG_DIR` (`~/.config/oikb`, ephemeral in-container), oikb's sync
  history was lost every recreation, so every restart re-synced from scratch.
  Fixed by the `OIKB_CONFIG_DIR=/data` + `data/oikb` chown change above.
- **`test_09_gdrive_index` could read oikb status mid-cycle.** The oikb source
  status check was a one-shot read; the daemon syncs every 30s, so it could
  land on `running` (transient) on an otherwise-healthy indexer and fail the
  test. It now polls up to 60s for a completion state
  (`success`/`ok`/`partial`); only a stuck `running` or `error` is a failure.
- **OWUI Chroma fd exhaustion under bulk ingest.** OWUI's RAG store (Chroma
  1.5.x, rust backend) opens a SQLite db per collection; the first gdrive sync
  creates 100s of collections and exhausted the default 1024 fd soft limit →
  `SQLITE_CANTOPEN "unable to open database file"` on every insert (KB
  `file_count` stayed 0, oikb uploads timed out, OWUI went unhealthy). Raised
  `ulimits.nofile.soft` to 65536 (hard 524288) on the `openwebui` service.
  Latent bug — OWUI RAG was not exercised at scale before.
- **oikb `concurrency` type crash.** oikb's `${VAR:-default}` interpolation
  returns env values as strings; the daemon compares `concurrency > 1` without
  coercing, so an interpolated `concurrency: ${OIKB_CONCURRENCY:-4}` crashed
  every sync with `'>' not supported between instances of 'str' and 'int'`.
  `concurrency` is now a literal YAML int (4) in `.oikb.yaml`; `interval` and
  `max-size` stay env-tunable (oikb string-parses them). `OIKB_CONCURRENCY`
  removed from compose/env.
- **`gdrive-status` / `test_09` read `file_count` from the wrong endpoint.**
  `GET /api/v1/knowledge/{id}` (detail) has neither `file_count` nor a populated
  `files` array; `file_count` is exposed only on the list endpoint
  (`GET /api/v1/knowledge/`). Both now read it from the list endpoint (with the
  read-scoped agent key), fixing a permanent `file_count=0` / "not visible to
  agent key" misreport.
- **`test_09` allowlist regex matched 0 files.** `find -iregex` with `(a|b)`
  alternation uses Emacs regex by default (find's default), where `()` and `|`
  are literal. Added `-regextype posix-extended` so the source-count check and
  search query match the allowlist (was a false-SKIP).
- **`gdrive-status` ETA wording when plateaued.** When the sync plateaus at
  `status=partial` (duplicate-content / over-max-size files OWUI/oikb skip, not
  pending), it now reports "not pending" instead of "first sync still running".
- **`gdrive-status` did not surface oikb sync errors (wrong field name).** It
  read `src_state.get("error")` (singular) but oikb's `/health` returns `errors`
  (a list) and `warnings` (a list), so the error was always dropped — a sync
  stuck at `status=partial` with a per-cycle file-link error (e.g. OWUI rejects
  a file with `400`) reported nothing wrong. `gdrive-status` now reads
  `errors`/`warnings`, prints `errors=N (<first error>)` on the oikb source
  line, notes the error count in the "counts match" branch, and attributes a
  no-progress plateau to failing-to-link files (not to duplicate-content) when
  errors are present. Exit code is unchanged (`partial` stays a healthy steady
  state).
- **`gdrive-sync` report crashed on fail-fast (`total_excl` unbound).**
  `total_excl`/`total_dups` were computed only after the loop, but `print_drive_table`
  runs (via `write_report_and_exit`) on the fail-fast / empty-source paths inside
  the loop — under `set -u` that is an unbound-variable exit, so the required
  failure report was not written. They are now initialized before the loop.
- **`gdrive-sync` miscounted overwrites as COPY and missed deletes under
  `--backup-dir`.** The log parser only matched `Copied (replaced existing)` /
  `Deleted`, but `rclone sync --backup-dir` logs overwrites as `Moved` + `Copied
  (new)` and deletes as `Moved` + `Moved into backup dir`. So `UPDATE` was always
  0, deletes invisible, and `COPY` inflated by overwrites. The classifier now
  treats `Moved` as pending and resolves it to `UPDATE` (a fresh `Copied (new)`
  follows) or `DELETE` (nothing follows), keeping the old verbs as a fallback.
  Verified against a local `--backup-dir` sync.
- **`gdrive-sync` dropped failures whose path began with `Failed `.** The retry-
  summary skip used bare prefixes (`Attempt `, `There were `, `Failed `), so a
  real file named e.g. `Failed invoice.pdf` was dropped from "Files not
  downloaded". Now matched on the rclone summary's numeric form
  (`Attempt N/M`, `There were N …`) so real paths are kept.
- **`gdrive-sync` report file was mode `0644`, not owner-only.** Written after
  `normalize_perms` under umask `022`, the report was `0644` despite the stated
  owner-only policy. Now `chmod 600` after writing (parent dir is `0700`).

## [v1.1.0] — 2026-08-20

Graphiti memory is now functional with Ollama. v1.0.0's memory stack never
extracted facts (MCP transport + OpenAI Responses API client + wrong
endpoints). This release swaps to the Graphiti REST server with an injected
Ollama-compatible client, fixes the extraction footprint, and adds a
clean-state e2e harness.

### Major changes

- **Graphiti memory functional (MCP -> REST + client injection).** Replaced
  the `zepai/graphiti` MCP image (could not be exposed; its LLM client targets
  the OpenAI Responses API, which Ollama answers with no parseable entities
  -> extraction silently stored nothing) with
  `ghcr.io/dkhokhlov/graphiti-rest:0.29.3`. `gateway/mcp.py` (-180) ->
  `gateway/graphiti.py` (+149, stdlib urllib REST client). `graphiti/bootstrap.py`
  (+192) is mounted and run as the command; it overrides the FastAPI
  `get_graphiti` dependency to inject the stock `OpenAIGenericClient`
  (>= 0.29 defaults to `json_schema` structured outputs, enforced server-side
  by Ollama) at `temperature=0` + `OpenAIEmbedder(nomic-embed-text, 768)`,
  holds a process-level singleton (no per-request close, so the async
  `/messages` worker does not race a closed driver), and runs a robust worker
  loop that logs + isolates per-episode failures.
- **Extraction footprint fix (ctx-baked model).** The `/v1` endpoint ignores
  `num_ctx` in requests, so the context window is baked into the model via a
  Modelfile. New vars `OLLAMA_MODEL_BASE`, `OLLAMA_MODEL_CONTEXT` (8192),
  `MODEL_NAME=qwen2.5:14b-ctx8192`. The stock 14B at the default 32k loads
  ~53 GB and spills to CPU on a 22.5 GB GPU (extraction crawls / stalls);
  `num_ctx 8192` loads ~20 GB, fits the GPU; a fact is searchable in ~9 s warm
  (~30 s cold, model load).
  `OPENWEBUI_MODEL` must equal `MODEL_NAME` so only one 14B instance loads.
- **`make test-e2e` harness.** Destructive orchestrator: `clean-all` ->
  `bootstrap` -> restore creds -> `preflight` -> `start` -> health poll ->
  `admin-signup` -> `api-keys` -> `rag-config` -> `test`. Stashes / restores
  `OPENWEBUI_TEST_USER/PASSWORD` around the wipe; refuses (no wipe) if unset.
  New `make admin-signup` (idempotent OWUI admin signup) +
  `scripts/e2e-restore-creds.sh` (in-place KEY=VALUE restore, avoids a
  no-trailing-newline concatenation bug) + `tests/test_08_e2e.sh` (full
  gateway surface: whoami, status, add, search, delete-edge, delete-episode,
  forget, admin user-create + issued-key round-trip, non-admin deny).

### Operator-facing changes (action required on upgrade)

- **`OLLAMA_BASE_URL` -> `OLLAMA_HOST`** (Ollama's native client var). Set
  `OLLAMA_HOST` (full URL, e.g. `http://<ollama-host>:11434`) in your shell or `.env`;
  shell overrides `.env`. The Open WebUI container still receives
  `OLLAMA_BASE_URL` (the name OWUI reads on first boot), sourced from
  `${OLLAMA_HOST}`.
- **Model vars.** Add `OLLAMA_MODEL_BASE=qwen2.5:14b`,
  `OLLAMA_MODEL_CONTEXT=8192`, `MODEL_NAME=qwen2.5:14b-ctx8192`; set
  `OPENWEBUI_MODEL=qwen2.5:14b-ctx8192` (must equal `MODEL_NAME`). Run
  `make pull-models` (now pulls the base, creates the ctx variant via
  `PARAMETER num_ctx`, pulls the embedder) and `make restart`.
- **`GRAPHITI_IMAGE_TAG=0.29.3`** (was absent / MCP). If a differently-named
  14B variant is already loaded in Ollama with a long keep-alive, `ollama stop`
  + `ollama rm` the obsolete alias first, then recreate the graphiti container
  (the running process keeps its startup-time model name).

### Fixes

- **Group_id charset.** Graphiti rejects `user:<email>` (charset
  `[A-Za-z0-9_-]`). The gateway now sanitizes `user:<email>` ->
  `user-<sanitized-email>` on write and normalizes the boundary on forget;
  callers keep the logical `user:<email>` form.
- **LLM was hitting api.openai.com.** `compose.yml` now sets `OPENAI_BASE_URL`
  on the graphiti service so extraction reaches Ollama.
- **`clean-all` wipe.** OWUI (root) and Neo4j (neo4j uid) write bind-mount
  files the host user cannot delete, so `rm -rf ./data` aborted midway and left
  a stale `.env.local`. Now wipes `./data` as root via a throwaway `alpine`
  container so the wipe completes.
- **Reasoning-model failure mode.** `MODEL_NAME` must be non-reasoning; a
  reasoning model spends `max_tokens` on its thinking chain and emits no
  content -> `json.loads('')` -> silent extraction failure. Default is
  `qwen2.5:14b`.
- **Extraction-detection flakiness.** Numbers embedded as quantities in
  probes are non-deterministically rephrased out of fact text (even at
  `temperature=0`); tests now detect on the stable descriptive noun, not the
  run-id number.

### New

- **In-repo per-tool skills** under `skills/{claude,codex,opencode,pi}/`
  (per-tool `SKILL.md`; scripts symlink to `skills/claude/scripts`).
- `make preflight` verifies the model exists **and** its `num_ctx` matches
  `OLLAMA_MODEL_CONTEXT` (a wrong / absent value silently reverts to 32k /
  CPU spill).
- `docs/agents.md`; README + `docs/operations.md` rewritten for the REST
  backend, the injection, the num_ctx model lifecycle, and new
  troubleshooting rows (slow-extraction / CPU-spill, reasoning-model empty
  response).

### Tests

`make test-e2e` green from clean state: test_01..08, 59 assertions, 0 failed.

## [v1.0.0] — 2026-08-20

Initial tagged release. MCP-based Graphiti memory stack (memory extraction
non-functional with Ollama — fixed in v1.1.0).

[v1.4.0]: https://github.com/dkhokhlov/knowledgebase/releases/tag/v1.4.0
[v1.1.0]: https://github.com/dkhokhlov/knowledgebase/releases/tag/v1.1.0
[v1.0.0]: https://github.com/dkhokhlov/knowledgebase/releases/tag/v1.0.0