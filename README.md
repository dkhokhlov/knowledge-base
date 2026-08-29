# KnowledgeBase

[![Graphiti](https://img.shields.io/badge/Graphiti-REST-blue)](https://github.com/getzep/graphiti)
[![Open WebUI](https://img.shields.io/badge/Open_WebUI-RAG-orange)](https://github.com/open-webui/open-webui)
[![Neo4j](https://img.shields.io/badge/Neo4j-5.26-green)](https://neo4j.com/)
[![Caddy](https://img.shields.io/badge/Caddy-gateway-1f83c7)](https://caddyserver.com/)
[![Ollama](https://img.shields.io/badge/Ollama-host_LLM-000000)](https://ollama.com/)
[![embed](https://img.shields.io/badge/embed-nomic--embed--text-brightgreen)](https://huggingface.co/nomic-ai/nomic-embed-text)
[![LLM](https://img.shields.io/badge/LLM-qwen2.5_14b-blueviolet)](https://ollama.com/library/qwen2.5)
[![OCR](https://img.shields.io/badge/OCR-deepseek--ocr-76b900)](https://ollama.com/library/deepseek-ocr)
[![extraction](https://img.shields.io/badge/extraction-markitdown--ocr-5c2d91)](https://github.com/microsoft/markitdown/tree/main/packages/markitdown-ocr)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED)](https://docs.docker.com/compose/)
[![license](https://img.shields.io/badge/license-MIT-yellow)](./LICENSE)

Self-hosted, **agent-first** knowledge stack combining **two complementary knowledge bases (KBs)** in one system. [Open WebUI][open-webui] provides a **document KB**: ingest curated documents — synced from **Google Drive** — into **access-controlled collections**; query by **RAG** (LLM-grounded answer) or **precise text / raw chunk retrieval** (no LLM). [Graphiti][graphiti] provides a **temporal fact memory** over [Neo4j][neo4j]: facts are **time-bound** and are **invalidated, not deleted**, when superseded, so the graph keeps history instead of only the latest snapshot. Model + motivation: [docs/memory.md](docs/memory.md).

Documents provide **grounded answers** from a **curated reference corpus**. **Fact memory** replaces scattered, untrimmed README, notes, and tracker files across projects with one **searchable temporal graph**, so accumulated knowledge stays findable and stops paying a growing **token tax** on every context load (details: [docs/memory.md](docs/memory.md)). Agents use the stack through the stack-side `api-gateway`, which authorizes **per-account** `KB_API_KEY` credentials with **identity and role** derived server-side, plus a **thin zero-dependency CLI** and the `/kb` skill. Humans can also use the Open WebUI web interface.

The document KB also indexes **[Claude Code's project memory][claude-code]** — the **per-project auto-memory** Claude Code writes under `~/.claude/projects/*/memory/` — into **one Open WebUI KB per project**, so knowledge accumulated across **sessions and repositories** stays **searchable** instead of expiring with each session's context. The `/kb` skill drives this **host-side** with the caller's own **user key** (`index-projects` / `retrieve-projects` / `status-projects`); the caller **creates and owns** each project KB, and the **api-gateway is not involved** on this surface. See [Projects memory indexing](docs/operations.md#projects-memory-indexing-claude-project-memory--open-webui) in docs/operations.md, and the `/kb` skill ([docs/agents.md](docs/agents.md)).

- **[Open WebUI][open-webui]** — document knowledge base with vector search, grounded RAG chat, and user/group access control; also the identity provider for the api-gateway.
- **[Graphiti][graphiti]** — temporal fact memory over [Neo4j][neo4j]; reached via an internal REST server.
- **[Neo4j][neo4j]** — graph store for [Graphiti][graphiti] (internal only).
- **api-gateway** — a custom component in this repo: stack-side authorization, per-account identity and role validation, Graphiti REST bridge, live group discovery, and admin user provisioning (zero-dependency Python stdlib).
- **[Caddy][caddy]** — the single public edge (`KB_HOST`): fronts Open WebUI at the root (catch-all) and proxies `/memory/*`, `POST /admin/users`, `POST /index`, `GET /status`, `GET /openapi.json`, `/health` to the api-gateway (method-scoped routes fall through to Open WebUI for other methods, so browser deep-links keep working).

[Ollama][ollama] supplies the chat LLM and [`nomic-embed-text`][nomic-embed-text] embeddings; it is reached via `OLLAMA_HOST` (Ollama's native client env var) and can run on the [Docker][docker] host or a remote/LAN host.

## Documentation map

README:

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Operating model](#operating-model)
- [Performance](#performance)
- [Security](#security)
- [Repository layout](#repository-layout)
- [Notes](#notes)

Sub-documents:

- [docs/operations.md](docs/operations.md) — prerequisites, configuration (env vars), Ollama host service, persistent data / RAID, make targets, troubleshooting, full hardening reference.
- [docs/ocr.md](docs/ocr.md) — markitdown-OCR external extraction engine: per-figure `deepseek-ocr` via native Ollama `/api/chat`, per-page/per-slide/per-sheet metadata, `OCR_ENABLED` config flag + scope + limits.
- [docs/testing.md](docs/testing.md) — integration test suite + matrix.
- [docs/agents.md](docs/agents.md) — agent integration per tool (Claude Code, Codex, OpenCode, Pi): install the `kb` skill, set `KB_HOST` + `KB_API_KEY`, example flows.
- [docs/memory.md](docs/memory.md) — fact memory (Graphiti): temporal model (time-stamped episodes, time-bound facts, invalidation) + motivation.

## Architecture

```
  ┌───────────┐       ┌────────────────────────┐
  │ User      │       │ Agent  (any host)      │
  │ (browser) │       │ kb_gateway.py / owui.py│
  │           │       │ KB_HOST + KB_API_KEY   │
  └──────┬────┘       └────────────┬───────────┘
         └─────────┬───────────────┘
                   ▼ KB_HOST  http://<host>:3000
  ┌────────────────┼─────────────────────────────────────────────────────────────┐
  │                │                                           Docker host       │
  │  ┌─────────────┴──────────────────────────────────────────────────────────┐  │
  │  │Caddy  :3000   KB_HOST — single public edge (only published port)       │  │
  │  │/memory/*  /admin/users(POST)  /health   → api-gateway :8010            │  │
  │  │/* catch-all                          → openwebui   :8080               │  │
  │  └─────────────┬─────────────────────────────────────┬────────────────────┘  │
  │                ▼                                     ▼                       │
  │  ┌─────────────┴────────────┐   ┌────────────────────┴────────────────────┐  │
  │  │openwebui :8080           │   │api-gateway :8010  (zero-dep stdlib)     │  │
  │  │owui_net                  │   │identity+role from KB_API_KEY via OWUI   │  │
  │  │identity + users          │   │owui_net ► openwebui :8080               │  │
  │  │+ RAG                     │   │graph_internal ► graphiti :8000          │  │
  │  │                          │   │graph_internal ► Neo4j :7474/7687        │  │
  │  │                          │   └─────────┬──────────────────────┬────────┘  │
  │  │                          │             ▼                      ▼           │
  │  │                          │   ┌─────────┴─────────┐   ┌────────┴────────┐  │
  │  │                          │   │graphiti :8000     │   │Neo4j :7474/7687 │  │
  │  │                          │   │(REST) graph_int   │   │group discovery  │  │
  │  │                          │   │bootstrap.py:      │   │delete guards    │  │
  │  │                          │   │ Ollama client     │   │(graph_int)      │  │
  │  │                          │   │ + nomic embed     │   │                 │  │
  │  └─────────────┬────────────┘   └─────────┬─────────┘   └─────────────────┘  │
  │                ▼                          ▼                                  │
  │  ┌─────────────┴──────────────────────────┴───────────────────────────────┐  │
  │  │Ollama  :11434   (host, via host-gateway)                               │  │
  │  │qwen2.5:14b-ctx8192 (chat + extraction, non-reasoning)  nomic-embed-text│  │
  │  └────────────────────────────────────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────────────────────────────────┘
```

- A user with a browser and an agent both reach the stack through **one** URL, **`KB_HOST`** (default `http://localhost:3000`): [Caddy][caddy] fronts [Open WebUI][open-webui] at the root and the **api-gateway** under `/memory/*`, `POST /admin/users`, and `/health`. One port, one var for agents (mirrors `OLLAMA_HOST`).
- An agent holds only `KB_API_KEY` + `KB_HOST` (no Graphiti token, no repo files) — it works on any host. Its CLI (`kb_gateway.py` / `owui.py`) is a thin client that reads ONLY those two env vars (no `.env` files) and hits `KB_HOST`: OWUI REST at `/api/*` (KBs, retrieve, files, projects memory), api-gateway at `/memory/*` (memory + RAG; the gateway inserts the chat model server-side).
- **api-gateway** is the sole bridge to the graph. It resolves the caller's identity + role from `KB_API_KEY` via Open WebUI (tamper-proof), enforces ownership-bounded writes + owner/admin destructive gating, discovers all existing groups live from [Neo4j][neo4j], calls the internal [Graphiti][graphiti] REST server, and provisions new KB users for admins.
- **Graphiti client injection.** The `ghcr.io/dkhokhlov/graphiti-rest` server defaults to `OpenAIClient` (OpenAI Responses API), which [Ollama][ollama] cannot satisfy — entity/fact extraction silently stores nothing. `graphiti/bootstrap.py` is mounted into the container and run as the command; it overrides the FastAPI dependency to inject the stock `OpenAIGenericClient` (graphiti_core >= 0.29 defaults to **`json_schema` structured outputs**, which Ollama enforces server-side; set at `temperature=0`) + `OpenAIEmbedder` (`nomic-embed-text`, 768-dim), so extraction works with Ollama. There is no config switch for this; the injection is required. See `graphiti/bootstrap.py`.
- **Network split**: `graph_internal` (neo4j + graphiti + api-gateway), `edge` (caddy + api-gateway), `owui_net` (caddy + api-gateway + openwebui). graphiti and Neo4j are **internal-only** — no host ports, reachable only through the gateway. Open WebUI is internal-only too (fronted by Caddy).
- Only `KB_HOST_PORT` (default `3000`) → Caddy `:3000` binds to the host. Caddy's `depends_on` uses `service_started` (not `service_healthy`) so the OWUI root stays reachable even if the gateway is broken.
- [Ollama][ollama] is external on the Docker host (reached via host-gateway); not published by this stack. [Open WebUI][open-webui] (RAG + chat) and graphiti (extraction LLM + embedder) both reach it.
- **TLS**: the Caddyfile is plain HTTP on the port. For a non-local `KB_HOST` you MUST either front Caddy with an upstream TLS reverse proxy, or switch the `:3000` site block to a hostname block so Caddy auto-terminates TLS and publishes `:443`. For the local MVP (`http://localhost:3000`) no TLS is needed.

## Quick start

**Host prerequisites:** `make`, `uv` (provisions the Python 3.12 `.venv` for the test suite — every test target runs `make ci`, which is `uv sync` from `pyproject.toml` + `uv.lock`; install at <https://docs.astral.sh/uv/>), `docker compose` (the stack), and `rclone` (only for `make gdrive-sync`).

Full prerequisites, configuration, and env vars are in [docs/operations.md](docs/operations.md). Core sequence:

**Minimum env vars to set** (in your shell env — both are commented out in `.env.template` so the shell value is not clobbered; everything else has a working default or is auto-generated):

- `OLLAMA_HOST`: the only hard blocker. `make start` fails fast if `OLLAMA_HOST` is unset — compose uses `${OLLAMA_HOST:?…}` (no `host.docker.internal` fallback; `make preflight` checks it too). `export OLLAMA_HOST=http://<ollama-host>:11434` (`http://host.docker.internal:11434` if Ollama runs on the Docker host). Do NOT use `localhost`/`127.0.0.1` — the value is used inside the containers, where localhost is the container's own loopback (no Ollama there).
- `KB_HOST`: the single public URL agents/clients point at. `export KB_HOST=http://<host>:3000` (defaults to `http://localhost:3000` when unset). `KB_HOST_PORT` (default `3000`) is the only host-published port. Use `https://<host>` / VPN for a remote agent (`KB_API_KEY` is a bearer — plain HTTP only on a trusted local interface).

**One-shot provision** — from a fresh checkout, run:

```
export OLLAMA_HOST=http://<ollama-host>:11434
export KB_HOST=http://<host>:3000
make provision
```

`make provision` chains the full first-time setup and leaves the stack running: `bootstrap` (creates `.env`/`.env.local` + the JWT key, `OCR_SERVICE_TOKEN`, first-user password, `./data` dirs; bakes `COMPOSE_PROFILES=ocr` into `.env` from `OCR_ENABLED`) → `pull-models` (BLOCKING: pulls the base LLM + ctx variant + embedder + `deepseek-ocr` from Ollama) → `start` (preflight + `docker compose up -d`; the ocr sidecar is included via `COMPOSE_PROFILES=ocr` in `.env`) → `admin-signup` (creates `admin@<KB_DOMAIN>`) → `api-keys` (admin + read-scoped agent keys; auto-configures OWUI → `markitdown-ocr` when `OCR_ENABLED=true`) → `rag-config` (strict-grounding RAG template + `rag.ollama.base_url` sync) → `gdrive-index-bootstrap` (creates the Google Drive KB + grants public read + writes `GDRIVE_KB_ID`). The JWT key, API keys, and Neo4j auth are generated locally; `OCR_ENABLED` defaults to `true` (to skip OCR: `make clean-all && make provision OCR_ENABLED=false`). Test-only credentials for `make test` are described in [docs/testing.md](docs/testing.md).

> **PROMPT for Agent**
>
> Paste this prompt into an agent (Claude Code, etc.); it runs the `make` targets for you (provision creates the 1st user `admin@<domain>`, this adds you as the 2nd). Edit the domain, your name, and email:
>
> ```
> Do make clean-all then provision from scratch with domain: <your.domain>;
> 2nd user (me) - <your-name>, <you>@<your.domain>;
> update my KB_API_KEY in ~/.bashrc.
> ```

**Populate the Google Drive KB** (one-time) — after `make provision`:

```
make gdrive-sync   # rclone pull into ./gdrive + POST /index (reconcile into the KB)
make gdrive-status # pretty JSON: completed/pending/processing/failed vs source (no ETA — no daemon)
```

See [Google Drive indexing (manual, via api-gateway)](#google-drive-indexing-manual-via-api-gateway). `make gdrive-sync` chains rclone → POST `/index`; indexing is manual/on-demand (no sidecar).

**Everyday restart** (provision is done once): `make start`. Re-assert after a DB reset: `make rag-config` (+ `make ocr-config`). Rotate keys: `make api-keys FORCE=1`.

(Optional) Close signup after the admin + agent accounts exist: set `ENABLE_SIGNUP=false` in `.env` and `make restart`.

## Operating model

The stack is **agent-facing**: the actor that calls the gateway is an **agent**. A **role** is `admin` or `user` (the Open Web UI role field). An **account** is an Open Web UI account that holds a role; `KB_API_KEY` is **per-account** (an Open Web UI API key).

- **Identity is tamper-proof.** The gateway resolves `(id, email, role)` from `KB_API_KEY` via Open Web UI `GET /api/v1/auths/`. The caller cannot set or influence it — there is no `KB_USER_ID` env var, no spoofable header.
- **Authorization = role + personal-group ownership**, both enforced on the stack (not bypassable by a modified CLI):
  - **Writes go to your own personal group.** `add` with no `--group` writes to `user:<email>`. `add --group G` is allowed only if `G` is your own personal group; any other group → `403`. There are **no shared write groups** — reads are how knowledge is shared across accounts. Graphiti stores the personal group as a charset-safe id (`user-<sanitized-email>`, e.g. `user-agent-local-test`); `forget` accepts the `user:<email>` form too.
  - **Reads span all groups that have data**, discovered live from [Neo4j][neo4j] (no roster file). `retrieve`, `episodes`, `status`, `groups` are read-only for everyone.
  - **Destructive ops** (`forget`, `delete-edge`, `delete-episode`) require owning the target group or admin. Admin (`role=admin`) overrides ownership and is the only role that can create users.

### Exposed endpoints

All endpoints are on one URL, **`KB_HOST`** (`http://<host>:3000` by default). Caddy routes by path.

| Endpoint | Auth | Use |
|---|---|---|
| `KB_HOST/` | session (web UI) | Open WebUI: document upload, chat, admin, users, knowledge bases |
| `KB_HOST/api/*` | `Authorization: Bearer <OWUI API key>` | OWUI REST for agents (files, KBs, projects memory); humans/admins also RAG directly here with an explicit `model` |
| `KB_HOST/docs` | none (read-only) | Swagger UI (OWUI; `ENV=dev`) |
| `KB_HOST/openapi.json` | none (read-only) | OpenAPI schema (OWUI) |
| `KB_HOST/memory/*` | `Authorization: Bearer <KB_API_KEY>` | api-gateway: memory (whoami, groups, add, retrieve, episodes, status, forget, delete-edge, delete-episode) + RAG chat (`POST /memory/rag`; the gateway inserts the chat model from `OPENWEBUI_MODEL`) |
| `KB_HOST/admin/users` | `Authorization: Bearer <KB_API_KEY>` (admin, POST) | api-gateway: create a new KB user (returns temp password + `KB_API_KEY`); GET falls through to the OWUI SPA |
| `KB_HOST/health` | none (read-only) | health probe (Caddy → api-gateway → OWUI, aggregated) |

`KB_HOST` is set in `.env` (default `http://localhost:3000`); `KB_HOST_PORT` (default `3000`) is the only host-published port. [Neo4j][neo4j] (`:7474`, `:7687`) and graphiti (`:8000` internal) are not published — reachable only through the api-gateway over `graph_internal`.

### Environment variable precedence

Two sourcing models:

- **Operator scripts** (`scripts/*.sh`, run via `make <target>`): source `.env`
  then `.env.local` (`set -a; . ./.env; . ./.env.local; set +a`). Precedence is
  `.env.local` > `.env` > shell env — the file wins (location-specific to the
  repo root). `KB_HOST` is computed from `KB_HOST_PORT`
  (`http://localhost:${KB_HOST_PORT}`) unless the shell sets `KB_HOST`.
- **The `/kb` skill** (`kb_gateway.py` / `owui.py`): a thin client that reads
  ONLY `KB_HOST` + `KB_API_KEY` from the shell env (no `.env` / `.env.local`
  sourcing) so it runs on any host.
- **Make-time tunables** (`make <target> VAR=val`) override the file: the script
  captures the shell value before sourcing and restores it after
  (see `scripts/api-keys.sh` for `KB_DOMAIN` / `OCR_ENABLED`). A target's own
  args (e.g. `EMAIL` / `NAME` / `QUERY` for `make users-*`) are not in `.env`,
  so they pass through unclobbered.

Full detail: [docs/operations.md](docs/operations.md) → Variable precedence.

### KB user provisioning (admin)

An admin runs **`make users-create EMAIL=alice@example.com NAME=Alice`** (an operator make target; the former in-skill `user-create` command is removed — admin functions are operator-only now). The make target calls the api-gateway `POST /admin/users` flow below.

- The gateway enforces `role=admin` **server-side** before any write. A non-admin key → `403` (not merely a CLI check). OWUI down → `503`.
- Flow (all inside the gateway, one stateless request): generate a strong temp password → create the OWUI user (`POST /api/v1/auths/add`, admin key) → sign in as the new user → generate that user's `KB_API_KEY` with the new user's own JWT (`POST /api/v1/auths/api_key`) → verify the key via `GET /api/v1/auths/` resolves to the expected email + `role=user`.
- Returns to the admin **only**: `email`, `temp_password`, `kb_api_key`, `role`, `id`. The gateway is stateless — it **never persists** the password or key; they exist only in the one response. The operator must relay them to the new account out-of-band and not store them.
- **Rollback**: if any step after user creation fails, the gateway deletes the partial user (admin `DELETE /api/v1/users/{id}`) and returns a clear error. It never reports success on partial provisioning. A duplicate email → deterministic `409` (no second account).
- Prerequisite: the deployed Open Web UI image must expose the provisioning endpoints. The gateway probes `/openapi.json` at startup and returns `501` from `/admin/users` if the image lacks them.

### RAG governance

- A user's uploaded files and knowledge bases are private to that user by default. The KB owner (with `sharing.knowledge`) or an admin grants a KB to user groups: Workspace -> Knowledge.
- An agent using a user's API key inherits that user's permissions: the user's own files + KBs shared with the user's groups. It cannot see other users' private docs. An admin key bypasses access control — give agents a dedicated low-priv user's key, not an admin key.
- To RAG a curated doc set: create a KB -> add docs -> grant it to a group -> put the agent's user in that group -> pass the KB in the `files` field as `{"type":"collection","id":"<kb-id>"}`. A top-level `knowledge` field is ignored, and `metadata.knowledge` is discarded server-side — only `files` grounds. The `/kb` skill reaches RAG via `POST /memory/rag` (the api-gateway inserts the chat model from `OPENWEBUI_MODEL`; send `messages` + `files`, no `model`); humans/admins RAG directly at `POST /api/chat/completions` with an explicit `model`.
- `make rag-config` sets a **strict-grounding RAG template** (admin config, persisted in `webui.db`): answer only from the retrieved context; refuse when the answer is absent; do not use outside knowledge or invent names/artifacts. The default template lets the model fall back to its own knowledge, which makes ~12B models confabulate. Re-run after any DB reset/rebuild. Grounding (chunk injection) is the caller's job (`files` field); this template governs what the model does with the chunks. It also syncs `rag.ollama.base_url` to `OLLAMA_HOST` (which OWUI otherwise leaves stale after a host change; `make preflight` warns on drift).

### Google Drive indexing (manual, via api-gateway)

The `gdrive` knowledge base is indexed by **api-gateway** (stateless, no sidecar). `make gdrive-sync` runs rclone to sync `./gdrive` from the shared drives, then POSTs `/index` to api-gateway, which walks `./gdrive` read-only and drives OWUI's native sync/diff protocol to reconcile the tree into the KB. Indexing is **manual/on-demand only**: no daemon, no schedule, no hooks. **Prerequisite:** a configured + authenticated rclone `gdrive` remote — one-time `rclone config` (new `gdrive` remote, Google Drive storage, browser OAuth login); `make gdrive-sync` fail-fasts if no shared drives are visible. Full setup (headless auth, verify, re-auth): see [docs/gdrive.md](docs/gdrive.md).

- `make gdrive-sync` syncs `./gdrive` from all shared drives (rclone `sync --backup-dir --delete-after`; delta — files removed from Drive are deleted from `./gdrive`, and the next `/index` drops them from the KB via `sync/cleanup`; deleted/overwritten files are retained in a dated `./.gdrive-backup/<UTC-ISO>/` dir outside `./gdrive` as a recovery net; `make clean-backup` clears it). Per-drive non-downloadable files and global patterns (e.g. `*.tmp`) are read from the gitignored `./gdrive-exclude.conf` (INI format: `[<drive name>]` and `[*]` sections; format documented in the tracked `gdrive-exclude.conf.example`) and converted to `--exclude-from` per drive. It fail-fasts on any transfer error and writes a per-run report (`0600`) to `./gdrive/.sync-reports/sync-<UTC-ISO>.report` with a per-drive `remote`/`local`/`excluded`/`dups` table, a `COPY`/`UPDATE`/`DELETE` breakdown (per file; correct under `--backup-dir`), a "Files excluded" section (Drive files matching the exclude patterns), a "Duplicates ignored" section (Drive files rclone skipped because another file shares the same path — Drive permits duplicate names), and a "Files not downloaded" section (e.g. admin-protected / download-restricted Drive files, surfaced with their 403 reason). Downloadable files still transfer; exit code is non-zero if any drive had errors. After rclone, it POSTs `/index` and fail-fasts on a non-2xx response or `ok=false` (per-file errors logged to stderr). `--index-all` forces a full re-index instead of incremental.
- `make gdrive-index` runs POST `/index` alone (no rclone). Default incremental; set `INDEX_ALL=1` for a full re-index (drain + re-upload every file). Set `RETRY_PENDING=1` to also re-trigger stalled `pending` files (delete + re-upload; the default retries only `failed`).
- `make gdrive-index-bootstrap` (one-time, after `make api-keys`) creates the `gdrive` KB, grants public read (`user:*`) so every authenticated user can retrieve/RAG it, and writes `GDRIVE_KB_ID` to `.env.local`. Idempotent. Does NOT start a sidecar (there is none).
- `make gdrive-status` reads GET `/status` and emits **pretty JSON (indent=2)**: `source` (`gdrive`), `kb_id`, `source_count` (allowlisted `gdrive/` file count), `indexed_count` (files with `data.status=completed` — extracted, embedded, linked, searchable), `pending` (in extraction / OCR / GPU, or queued), `processing` (embedding + linking), `failed`, `failed_files` (`{filename, error}`), `pending_files` (`{filename, error}`), and the per-file `indexed_files` list. Key order: `indexed_files` first (the long list scrolls off the top), then `failed_files`/`pending_files`, then the single-field counts (visible at the bottom). The drain is terminal when `pending+processing=0` AND `completed+failed` covers `source_count`. No ETA (no daemon). (It passes `?json=1`; the bare `/status` text/glyph form still exists for direct curl.)
- Indexed file types are the documents-only allowlist hardcoded in `gateway/app.py` `DEFAULT_ALLOW` (source code is handled by open-codebase-index; `.npy`, audio/video, images, archives, `.svg`/`.drawio` are excluded). Max size is `KB_MAX_SIZE` (default `100mb`, `.env`). `/index` does incremental SHA-256 diffing against the live KB and **fails closed on an empty source** (0 files + not `?force=1` → 422, no `cleanup`), so a bad/empty mount cannot mass-delete the KB.
- api-gateway reaches OWUI internally (`openwebui:8080` on `owui_net`) with the admin key in its env (`OPENWEBUI_ADMIN_API_KEY`, injected from `.env.local`). The caller's `KB_API_KEY` is authorization only (identity via OWUI, role checked for `/index` admin). The gateway is stateless: no `history.db`, no `./data/oikb`. File bytes flow gateway → OWUI internally (not through Caddy); Caddy carries only the trigger + the results JSON.
- Per-file transparency: `/index` returns `{added, modified, deleted, unmodified, retried, errors, ok}` where `errors` carries `{filename, status, error}` per failed upload/dir-create/re-trigger — the diagnosis surface (no opaque daemon aggregate). The gateway does NOT link files itself: `POST /files/` queues OWUI's per-upload background task (extract → embed → link), which is the sole linker. `/index` re-triggers `failed` files every run (delete + re-upload; `--retry-pending` / `?retry_pending=1` also re-triggers `pending`). `ok=false` means a real upload/extract error (the upload-idempotency + path-aware-dedup patches make duplicate-content 400s not occur).
- Subpath reconcile: `/index` and `/status` accept `?path=<relpath>` (relative to the Google Drive root; a directory or a single file) — exposed as `make gdrive-sync SCOPE_PATH=<relpath>` / `make gdrive-index SCOPE_PATH=<relpath>` / `make gdrive-status SCOPE_PATH=<relpath>` (the env var is `SCOPE_PATH`, not `PATH`, to avoid clobbering the shell's executable-search `PATH`). `path` is a SOURCE FILTER only: it scopes the walk to that subpath (entry keys stay root-relative, so a later full `/index` sees `unmodified`). The reconcile is a FULL reconcile of that subpath — files removed from the source under it ARE removed from the KB — so use a KB whose whole scope is that `path` (a dedicated/subpath KB; on a shared KB `path` deletes every KB file outside the subpath). `/status?path=` scopes `source_count` to the subpath (file counts are KB-wide). The full walk skips dot-dirs/dot-files (hidden metadata like `.sync-reports`/`.sync.lock` is never indexed); `path` opts into a dot-subtree.

### OCR extraction (image-bearing documents)

The `markitdown-ocr` sidecar is an **external extraction engine** that OCRs image-bearing documents (PDF/DOCX/PPTX/XLSX) and standalone images via `deepseek-ocr` on Ollama's native `/api/chat`, so image-only PDFs and embedded figures/diagrams become searchable instead of orphaning. Gated by `OCR_ENABLED` in `.env` (default `true`); compose-profile-gated (`COMPOSE_PROFILES=ocr`, baked into `.env` by `make bootstrap` from `OCR_ENABLED` and read by `docker compose` for every command); no per-type fallback (global + all-or-nothing). When enabled it is provisioned by the standard chain (`bootstrap` generates the token + bakes `COMPOSE_PROFILES=ocr`, `pull-models` pulls `deepseek-ocr`, `start` builds + starts the sidecar, `api-keys` sets the OWUI routing) — no separate step. To disable, run `make clean-all && make provision OCR_ENABLED=false` (bakes `OCR_ENABLED=false` + an empty `COMPOSE_PROFILES` into `.env`; existing OCR'd members keep their content until re-ingested). A hit carries `file_id` + `page` → the exact original page/slide/sheet. Full design, scope, and service guards: [docs/ocr.md](docs/ocr.md).

For per-tool agent integration (skill install, CLI examples), see [docs/agents.md](docs/agents.md).

## Performance

Measured warm on the GPU host (one 14B ctx-baked model loaded). The first call after idle adds model-load time (~30 s cold). RAG chat and async extraction include the LLM; the other operations do not.

### Memory (api-gateway)

| Operation | Endpoint | Median latency |
|---|---|---|
| add (queue -> 202, episode queued) | `POST /memory/add` | ~54 ms |
| retrieve facts | `POST /memory/retrieve` | ~89 ms |
| fetch raw episodes | `GET /memory/episodes?max=20` | ~57 ms |
| add -> fact searchable (async extraction, warm model) | add + poll `/memory/retrieve` | ~9 s |

- `/memory/retrieve` returns **facts** (`entity_edges`; fact text + `valid_at` / `invalid_at`) via Graphiti RAG: the query is embedded (`nomic-embed-text`), matched by vector similarity over nodes, then refined by graph traversal. The original `add` text is not returned.
- `/memory/episodes` returns the raw **episodes**: the original `add` texts, verbatim, in order. No embedding, no LLM, no graph traversal.

### RAG (Open Web UI)

| Operation | Endpoint | Median latency |
|---|---|---|
| raw chunk retrieval | `POST /api/v1/retrieval/query/collection` | ~64 ms (first call ~328 ms) |
| RAG chat (retrieval + grounded LLM) | `POST /api/chat/completions` (`files`) | ~1.5 s |

- Raw retrieval (`/api/v1/retrieval/query/collection`) returns **chunks**: a Chroma-style object (`distances`, `documents`, `metadatas`; OWUI omits the `ids` array — identify a chunk by `file_id` + `start_index` in its metadata), top-k by vector similarity. No LLM runs.
- RAG chat (`/api/chat/completions` with `files:[{"type":"collection","id":<kb-id>}]`) runs retrieval, injects the chunks as context, and returns a grounded LLM answer. The `make rag-config` strict template makes the model answer only from the retrieved chunks and refuse when the answer is absent.

## Security

The trust model in brief. For lockdown defaults, phone-home hardening, container caps, secrets handling, Neo4j auth, and dev-mode docs, see [docs/operations.md#hardening-reference](docs/operations.md#hardening-reference).

### Open WebUI (fronted by Caddy at `KB_HOST`)

- `WEBUI_AUTH=true`: every UI and REST call needs a session or a Bearer API key.
- First registered user becomes admin. Later signups get `DEFAULT_USER_ROLE=pending` and need admin approval. Close signup with `ENABLE_SIGNUP=false` + `make restart`.
- Knowledge bases are private by default; admins grant access to user groups (Workspace -> Knowledge). RBAC is additive: role + group membership.
- JWTs signed with a generated secret key (gitignored `.env.local`; `make start` rejects an empty/missing key). See [docs/operations.md#secrets-handling](docs/operations.md#secrets-handling).

### api-gateway (`KB_HOST/memory/*`, `/admin/users`, `/health` → `:8010`)

- graphiti and Neo4j have no agent-facing auth and are **internal-only** on `graph_internal`. The api-gateway is the sole bridge; [Caddy][caddy] (`KB_HOST`) proxies `/memory/*`, `POST /admin/users`, and `/health` to it. The Graphiti REST server has no native auth — the gateway is the gate.
- Every gateway endpoint (except `/health`) requires `Authorization: Bearer <KB_API_KEY>`. No key → `401`; bad key → `401`; Open WebUI unreachable → `503` (fail closed).
- `/health` is ungated on purpose: non-sensitive probe, carries no data or credentials. It also probes Open WebUI so `make health` catches an identity-broken stack rather than reporting healthy while auth is down.
- `KB_API_KEY` is a bearer — `KB_HOST` MUST be HTTPS or VPN/tunnel for any non-local agent. Plain HTTP is safe only on a trusted local interface.
- Rotate [Open WebUI][open-webui] API keys on a schedule (`FORCE=1 make api-keys`).

## Repository layout

| Path | Contents |
|---|---|
| `compose.yml` | services + networks (`graph_internal`, `edge`, `owui_net`); api-gateway internal env |
| `gateway/` | api-gateway: `app.py`, `authorize.py`, `graphiti.py`, `neo4j.py`, `owui.py`, `Dockerfile` (zero-dependency stdlib, non-root) |
| `caddy/Caddyfile` | public edge `KB_HOST`; routes `/memory/*`, `POST /admin/users`, `/health` → api-gateway:8010, catch-all → openwebui:8080 |
| `graphiti/bootstrap.py` | mounted into the `graphiti` container and run as the command; injects `OpenAIGenericClient` + `nomic` embedder (768) so Ollama extraction works, runs a robust `/messages` worker, owns the app lifespan |
| `graphiti/config.yaml` | UNUSED — config for the retired MCP image; kept for reference (not mounted) |
| `Modelfile_qwen2_5` | reference Modelfile for the custom ctx-baked `GRAPHITI_MODEL` (`FROM qwen2.5:14b` + `PARAMETER num_ctx 8192`); see [docs/operations.md](docs/operations.md#custom-model-ctx-baked-variant) |
| `scripts/` | `bootstrap.sh`, `api-keys.sh`, `preflight.sh`, `rag-config.sh`, `gdrive-sync`, `gdrive-index-bootstrap.sh` |
| `skills/` | per-tool `kb` agent skill (`claude/` primary; `codex/`, `opencode/`, `pi/` symlink `scripts/` to it) — see [docs/agents.md](docs/agents.md) |
| `tests/` | `test_01`..`test_08` + `lib.sh` (see [docs/testing.md](docs/testing.md)) |
| `docs/` | `operations.md`, `testing.md`, `agents.md`, favicon assets |
| `.env` / `.env.template` | tracked template — no secrets (ports, tags, models, tunables, `OLLAMA_HOST`) |
| `.env.local` / `.env.local.template` | gitignored secrets + generated keys (`chmod 0600`) |
| `Makefile` | targets (see [docs/operations.md#make-targets](docs/operations.md#make-targets)) |
| `LICENSE` | MIT |

## Notes

- Embedding dimensions must be 768 for [`nomic-embed-text`][nomic-embed-text]. Change `EMBEDDER_MODEL` and `EMBEDDER_DIMENSIONS` together if you swap models. The bootstrap reads `EMBEDDER_DIMENSIONS` to set both the embedder and the vector index dim (Graphiti defaults to 1024, which would reject 768-dim writes).
- [Graphiti][graphiti] extraction uses **`json_schema` structured outputs** over Chat Completions. The image's default client targets the OpenAI Responses API (Ollama cannot satisfy it — extraction silently stores nothing), so `graphiti/bootstrap.py` injects the stock `OpenAIGenericClient` (>= 0.29 defaults to `json_schema`, which Ollama enforces server-side) at `temperature=0`. With plain `json_object` mode a 14B local model echoes the response schema back as values (Neo4j then rejects the nested MAP); `json_schema` prevents that.
- Extraction is **async**: `POST /memory/add` returns `200` as soon as the episode is queued, but each episode runs several LLM extraction calls before a fact is searchable. With the ctx-baked model (`num_ctx=8192`, ~20 GB, fits the GPU) a fact is searchable in ~9 s warm (~30 s cold, model load); do not treat a bare `200` (or even a stored episode) as proof of extraction — poll `/memory/retrieve` for the probe.
- The extraction LLM (`GRAPHITI_MODEL`, built from `OLLAMA_MODEL_BASE`) **must be a non-reasoning model.** Graphiti 0.29.3 calls it with `max_tokens=16384`; a reasoning model (one with a thinking chain) spends the budget on its thinking and emits no `content` (`finish_reason=length`) → `json.loads('')` → extraction silently stores nothing, even with `json_schema` enforcement. `qwen2.5:14b-ctx8192` (the default `GRAPHITI_MODEL`) is non-reasoning and tested reliable. Ollama's `/v1/chat/completions` does not honor `think=false`, so suppressing reasoning that way is not an option.
- The `/v1` endpoint ignores `num_ctx` in the request body, so `make pull-models` bakes `OLLAMA_MODEL_CONTEXT` (default `8192`) into `GRAPHITI_MODEL` via a `PARAMETER num_ctx` Modelfile (see [`Modelfile_qwen2_5`](Modelfile_qwen2_5) and [docs/operations.md](docs/operations.md#custom-model-ctx-baked-variant)). The remote GPU host (~22.5 GB VRAM) cannot hold the stock 14B at the default 32k context (~53 GB) — it spills to CPU and extraction crawls; `num_ctx=8192` loads ~20 GB and fits. `OPENWEBUI_MODEL` (chat) and `GRAPHITI_MODEL` (extraction) are independent; the defaults are equal so one 14B instance loads, but they may differ. `make preflight` verifies the extraction model exists and its `num_ctx` matches.
- There are no shared write groups. Each account writes to its own personal group (logical `user:<email>`, stored by Graphiti as `user-<sanitized-email>` e.g. `user-agent-local-test`); reads span all groups, so cross-account knowledge is shared read-only.
- [Open WebUI][open-webui] exposes `/docs` and `/openapi.json` only in `ENV=dev` (also raises log verbosity — acceptable for an internal/LAN deployment).
- `SEMAPHORE_LIMIT=3` bounds graphiti_core extraction concurrency; conservative for one local [Ollama][ollama]. Raise it if the host Ollama has capacity.

[graphiti]: https://github.com/getzep/graphiti
[open-webui]: https://github.com/open-webui/open-webui
[neo4j]: https://neo4j.com/
[caddy]: https://caddyserver.com/
[ollama]: https://ollama.com/
[docker]: https://www.docker.com/
[nomic-embed-text]: https://huggingface.co/nomic-ai/nomic-embed-text
[claude-code]: https://code.claude.com/docs/en/memory