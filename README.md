# KnowledgeBase

[![Graphiti](https://img.shields.io/badge/Graphiti-MCP-blue)](https://github.com/getzep/graphiti)
[![Open WebUI](https://img.shields.io/badge/Open_WebUI-RAG-orange)](https://github.com/open-webui/open-webui)
[![Neo4j](https://img.shields.io/badge/Neo4j-5.26-green)](https://neo4j.com/)
[![Caddy](https://img.shields.io/badge/Caddy-gateway-1f83c7)](https://caddyserver.com/)
[![Ollama](https://img.shields.io/badge/Ollama-host_LLM-000000)](https://ollama.com/)
[![MCP](https://img.shields.io/badge/MCP-Streamable_HTTP-8b5cf6)](https://modelcontextprotocol.io/)
[![embed](https://img.shields.io/badge/embed-nomic--embed--text-brightgreen)](https://huggingface.co/nomic-ai/nomic-embed-text)
[![LLM](https://img.shields.io/badge/LLM-gemma4_12b-blueviolet)](https://ollama.com/library/gemma4)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED)](https://docs.docker.com/compose/)
[![license](https://img.shields.io/badge/license-MIT-yellow)](./LICENSE)

Self-hosted knowledge base stack. [Ollama][ollama] runs on the [Docker][docker] host and supplies
the chat LLM and [`nomic-embed-text`][nomic-embed-text] embeddings.

- **[Graphiti MCP][graphiti]** — temporal knowledge graph over [Neo4j][neo4j]; HTTP [MCP][mcp] server.
- **[Open WebUI][open-webui]** — document chat with RAG and user/group access control; REST API for agents.
- **[Neo4j][neo4j]** — graph store for [Graphiti][graphiti] (internal only).
- **[Caddy][caddy] gateway** — bearer-token gate in front of Graphiti MCP.

## Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Ollama host service](#ollama-host-service)
- [Exposed endpoints](#exposed-endpoints)
- [Security](#security)
- [Agents](#agents)
- [Persistent data and moving to RAID](#persistent-data-and-moving-to-raid)
- [Tests](#tests)
- [Make targets](#make-targets)
- [Notes](#notes)

## Architecture

```
      ┌─────────────┐         ┌─────────────┐                     ┌─────────────┐
      │ User        │         │ Agent       │                     │ Agent       │
      │ (browser)   │         │ (REST)      │                     │ (MCP)       │
      └─────────────┘         └─────────────┘                     └─────────────┘
             │ HTTP                  │ REST /api/*                       │ MCP /mcp/
             │                       │ Bearer OWUI key                   │ Bearer token
┌────────────▼───────────────────────▼───────────────────────────────────▼─────────────────┐
│ Docker host  (kbnet bridge)                                                              │
│  ┌──────────────────────────────────────────────────┐    ┌────────────────────────────┐  │
│  │ Open WebUI  :3000  (HTTP + REST)                 │    │ Caddy gateway :8000        │  │
│  │                                                  │    │ bearer-token gate          │  │
│  │  ┌─────────────┐   ┌──────────────────────────┐  │    │ /health ungated            │  │
│  │  │ Chat UI     │   │ Knowledge base           │  │    └─────────────┬──────────────┘  │
│  │  │ (RAG chat)  │◄─►│ (indexed                 │  │                  │                 │
│  │  │             │   │  documents)              │  │                  │                 │
│  │  └──────┬──────┘   │                          │  │    ┌─────────────▼─┐   ┌────────┐  │
│  │         │          │                          │  │    │ graphiti-mcp  │   │Neo4j   │  │
│  │         │          │                          │  │    │ (internal)    ├──►│(graph) │  │
│  │         │          └────────────┬─────────────┘  │    │ LLM + embedder│   │internal│  │
│  │         │                       │                │    │               │   │        │  │
│  │         │                       │                │    └────────┬──────┘   └────────┘  │
│  │         │                       │                │             │                      │
│  └─────────┼───────────────────────┼────────────────┘             │                      │
│            │                       │                              │                      │
│            │                       │                              │                      │
│  ┌─────────▼───────────────────────▼──────────────────────────────▼───────────────────┐  │
│  │ Ollama  :11434  (host, via host-gateway)                                           │  │
│  │ gemma4:12b (chat LLM)     nomic-embed-text (embeddings                             │  │
│  │                                                                                    │  │
│  └────────────────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

- A user with a browser reaches [Open WebUI][open-webui] over HTTP (`:3000`).
- An agent uses Open WebUI over REST (`:3000/api/*`, Bearer OWUI API key) and Graphiti over [MCP][mcp] (`:8000/mcp`, Bearer `GRAPHITI_API_TOKEN`).
- Inside [Open WebUI][open-webui]: the Chat UI (RAG chat) reads from the Knowledge base (indexed documents); both reach [Ollama][ollama] for the chat LLM and embeddings.
- [Graphiti MCP][graphiti] reaches [Ollama][ollama] for its LLM + embedder and [Neo4j][neo4j] for the graph store.
- Only `:3000` ([Open WebUI][open-webui]) and `:8000` (graphiti gateway) bind to 0.0.0.0.
- [Neo4j][neo4j] and graphiti-mcp are container-network only (no host ports).
- [Ollama][ollama] is external on the Docker host (reached via host-gateway); not published by this stack.
- The [Caddy][caddy] gateway checks `Authorization: Bearer <GRAPHITI_API_TOKEN>` on `/mcp`.

## Prerequisites

- [Docker][docker] >= 20.10 with the [Compose][docker-compose] plugin (`docker compose`).
- [Ollama][ollama] reachable from the containers. Set `OLLAMA_BASE_URL` in `.env` (`.env` is gitignored; `make bootstrap` creates it from `.env.example`, or `cp .env.example .env`):
  - `http://host.docker.internal:11434` if Ollama runs on the Docker host.
  - `http://<ollama-host>:11434` if Ollama is a remote host on the LAN.
- Pull the models you will use:
  ```
  make pull-models        # pulls MODEL_NAME (default gemma4:12b) + nomic-embed-text
  ```
  Equivalent to `ollama pull <MODEL_NAME>` and `ollama pull nomic-embed-text`.

## Quick start

1. Bootstrap the local secret file and data dirs:
   ```
   make bootstrap
   ```
   - Generates both secrets (`WEBUI_SECRET_KEY`, `GRAPHITI_API_TOKEN`) into `.env.local`.
   - Locks `.env.local` to `0600`.
   - Creates `./data/{neo4j/data,neo4j/logs,openwebui}`.
   - Prints `GRAPHITI_API_TOKEN` to the terminal.
   - Creates `.env` from `.env.example` (gitignored) if it does not exist.
   - Set `OLLAMA_BASE_URL` in `.env` to your Ollama host (see [Prerequisites](#prerequisites)) before `make start`.
2. Copy the printed `GRAPHITI_API_TOKEN` into your [MCP][mcp] clients (sent as `Authorization: Bearer <token>`).
   - `WEBUI_SECRET_KEY` is internal to [Open WebUI][open-webui]; no action needed.
   - It is a random JWT signing key, needed before `make start` (Open WebUI will not boot without it).
   - It is unrelated to user accounts/login; the first user registers later in step 7.
   - Do not change it after users exist, or all sessions are invalidated.
3. Pull the host [Ollama][ollama] models (if not already pulled):
   ```
   make pull-models
   ```
4. Check the environment:
   ```
   make preflight
   ```
5. Start the stack (auto-pulls Docker images if missing):
   ```
   make start
   ```
6. Verify:
   ```
   make health
   ```
7. Open `http://<your-host-ip>:3000` and register the first user.
   - The first user becomes the admin.
   - Set `OPENWEBUI_TEST_USER` / `OPENWEBUI_TEST_PASSWORD` in `.env.local` to this admin account (used by `make test` and `make api-keys`).
   - Later signups need admin approval (`DEFAULT_USER_ROLE=pending`).
8. Provision two REST API keys (admin + a read-scoped agent user) into `.env.local`:
   ```
   make api-keys
   ```
   - Enables API keys stack-wide and creates a dedicated non-admin user `agent@local.test`.
   - Writes `OPENWEBUI_ADMIN_API_KEY` (full admin — keep private) and `OPENWEBUI_USER_API_KEY` (read-scoped — hand to agents) into `.env.local`. See [API keys & agent access](#api-keys--agent-access).
   - Idempotent; set `FORCE=1` to rotate the keys.
   - Env vars the `kb` skill reads (if installed) — all already in `.env` / `.env.local`:

     | Var | Source | Purpose | Required |
     |---|---|---|---|
     | `OPENWEBUI_HOST_PORT` | `.env` | base URL `http://localhost:<port>` | yes |
     | `OPENWEBUI_USER_API_KEY` | `.env.local` | read-scoped agent key | yes |
     | `OPENWEBUI_MODEL` | `.env` | chat model for RAG | yes |

     Keep `OPENWEBUI_MODEL` in sync with `MODEL_NAME` (the skill does not read `MODEL_NAME`). Point the wrapper at both env files:
     ```
     python3 ~/.claude/skills/kb/scripts/owui.py --env-file .env --env-file .env.local kbs
     ```
9. Set the strict-grounding RAG template:
   ```
   make rag-config
   ```
   - Re-run after a DB reset/rebuild (`make clear-all` reverts it to the image default). See [RAG governance](#rag-governance).

Provisioning sequence: `make start` → (admin signs up in UI) → `make api-keys` → `make rag-config`.

10. (Optional) Close signup after bootstrap:
   - Set `ENABLE_SIGNUP=false` in `.env`.
   - Run `make restart`.

## Configuration

- `.env` — gitignored, copied from `.env.example` (the tracked template). No secrets; set `OLLAMA_BASE_URL` to your Ollama host here:
  - ports, image tags, model names, Neo4j memory, tunables, `OLLAMA_BASE_URL`.
- `OLLAMA_BASE_URL` — where [Graphiti MCP][graphiti] and [Open WebUI][open-webui] reach [Ollama][ollama].
  - [Graphiti][graphiti] reads it fresh on every start (compose interpolates `OPENAI_API_URL`).
  - [Open WebUI][open-webui] persists the Ollama URL in its DB on **first boot** and ignores later env changes.
    - Chat URL: admin API `POST /ollama/config/update` with `{"ENABLE_OLLAMA_API":true,"OLLAMA_BASE_URLS":["http://<ollama-host>:11434"],"OLLAMA_API_CONFIGS":{}}`, or wipe `./data/openwebui` and restart.
    - RAG embedding URL (`rag.ollama.base_url`) is a **separate** persisted key; the chat update above does **not** change it. A stale value leaves chat working but breaks file embedding (`process/status` = `failed`, RAG search returns 0 hits). Fix: admin API `POST /api/v1/retrieval/embedding/update` with `{"RAG_EMBEDDING_ENGINE":"ollama","RAG_EMBEDDING_MODEL":"nomic-embed-text","ollama_config":{"url":"http://<ollama-host>:11434","key":""}}`, or wipe `./data/openwebui` and restart. `make test` (test_04) catches this drift.
- `.env.local` — gitignored, `chmod 0600`. Holds the two secrets:
  - `WEBUI_SECRET_KEY` (generated by `make bootstrap`).
  - `GRAPHITI_API_TOKEN` (generated by `make bootstrap`, printed to the terminal; copy into [MCP][mcp] clients).
  - [Docker Compose][docker-compose] loads both via `env_file`.
- `graphiti/config.yaml` — [Graphiti MCP][graphiti] config:
  - LLM + embedder point at [Ollama][ollama] `/v1`.
  - [`nomic-embed-text`][nomic-embed-text]; 768 dimensions.
  - Mounted read-only into the container.
- `caddy/Caddyfile` — bearer-token gate:
  - Token read from [Caddy][caddy]'s env, so the Caddyfile is safe to commit.

## Ollama host service

Ollama runs on the Docker host as a systemd unit
(`/etc/systemd/system/ollama.service`). The stack reaches it at
`OLLAMA_BASE_URL=http://<ollama-host>:11434` (see `.env` and
[Prerequisites](#prerequisites)). Unit env params:

| Env var | Value | Purpose |
|---|---|---|
| `OLLAMA_HOST` | `0.0.0.0` | Listen on all interfaces (LAN-reachable). |
| `OLLAMA_KEEP_ALIVE` | `15m` | Keep models loaded for 15 min after the last request. |
| `OLLAMA_FLASH_ATTENTION` | `1` | Enable flash-attention KV paths (lower memory, faster). |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | 8-bit KV cache — halves KV memory vs fp16. |
| `OLLAMA_NUM_PARALLEL` | `12` | Up to 12 concurrent requests per loaded model. |
| `OLLAMA_NUM_GPU_LAYERS` | `999` | Offload all layers to GPU (model fits in VRAM). |
| `OLLAMA_CONTEXT_LENGTH` | `32000` | Max context per request (clamps the model native 262144). |
| `OLLAMA_DEBUG` | `1` | Verbose logs. |
| `OLLAMA_MAX_LOADED_MODELS` | (commented) | Optional cap on loaded models; disabled. |

Effective state (`ollama ps`, `gemma4:12b` active):

| Model | Size | Processor | Context |
|---|---|---|---|
| `gemma4:12b` | 17 GB | 100% GPU | 32000 |
| `nomic-embed-text:latest` | 323 MB | 100% GPU | 2048 |

Host: a CUDA GPU host with enough VRAM for the chat model weights plus the 12-slot KV cache.

Notes:
- `gemma4:12b` (11.9B, Q4_K_M, ~7.6 GB weights) + 8-bit flash-attention KV cache keeps the 12-slot, 32k-context buffer in VRAM, so the model stays 100% on GPU (no CPU offload).
- `OLLAMA_CONTEXT_LENGTH=32000` clamps the model native context (262144) per request.
- Keep `MODEL_NAME` in `.env` inside this VRAM budget; a larger model spills to CPU (50/50 split) and slows RAG.

## Exposed endpoints

| Endpoint | Auth | Use |
|---|---|---|
| `http://<host>:3000` | session (web UI) | document upload, chat, admin, users, knowledge bases |
| `http://<host>:3000/api/*` | `Authorization: Bearer <OWUI API key>` | REST for agents |
| `http://<host>:3000/docs` | none (read-only) | Swagger UI |
| `http://<host>:3000/openapi.json` | none (read-only) | OpenAPI schema |
| `http://<host>:8000/mcp` | `Authorization: Bearer <GRAPHITI_API_TOKEN>` | [MCP][mcp] (Streamable HTTP; `/mcp/` redirects here) |
| `http://<host>:8000/health` | none (read-only) | health probe |

- [Neo4j][neo4j] (`:7474`, `:7687`) and graphiti-mcp (`:8000` internal) are not published.

## Security

### Open WebUI (`:3000`)

- `WEBUI_AUTH=true`: every UI and REST call needs a session or a Bearer API key.
- First registered user becomes admin. Later signups get `DEFAULT_USER_ROLE=pending` and need admin approval.
- Closed instance: set `ENABLE_SIGNUP=false` in `.env` and `make restart`.
- Two user roles:
  - **Admin** (`admin@local.test`, first registrant) — full access; bypasses access control. API key: `OPENWEBUI_ADMIN_API_KEY`. **Do not give to agents.**
  - **Agent** (`agent@local.test`, non-admin) — created by `make api-keys`; read-scoped. API key: `OPENWEBUI_USER_API_KEY` — **hand this to agents**.
- Provision both keys with `make api-keys` (run after `make start` + admin signup). It enables API keys (`ENABLE_API_KEYS`) and non-admin API keys (`USER_PERMISSIONS_FEATURES_API_KEYS`), creates the agent user, and writes the keys into gitignored `.env.local`. See [API keys & agent access](#api-keys--agent-access).
  - Read-scoping: the agent key sees KBs via their `*` read grants and can search them, but cannot `file/add`, remove files, or delete a KB it does not own. `make test` (test_05) guards this.
  - For a hard API-layer read-only lock on every key: Settings -> Admin -> API Key Endpoint Restrictions (`ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS`). Global — also restricts the admin key.
- Knowledge bases are private by default. Admins grant access to user groups: Workspace -> Knowledge.
  - RBAC is additive: role + group membership.
- JWTs signed with `WEBUI_SECRET_KEY`:
  - Stable, stored only in gitignored `.env.local`.
  - `make start` rejects an empty/missing key (the guard is in the Makefile `start` target).

### Graphiti MCP (`:8000`)

- graphiti-mcp has no native auth, so the [Caddy][caddy] gateway requires `Authorization: Bearer <GRAPHITI_API_TOKEN>` on `/mcp`.
  - Requests without the token get `401`.
- `GRAPHITI_API_TOKEN` is a single shared secret (one token for all users).
  - Generated by `make bootstrap`, printed to the terminal, stored in gitignored `.env.local` (`chmod 0600`).
  - Caddy reads it from its env via `{$GRAPHITI_API_TOKEN}`, so the token never appears in the committed Caddyfile.
- Rotate the token: edit `.env.local`, then `make restart`.
- `/health` is ungated on purpose: non-sensitive probe, carries no data or credentials.
- Shared-token means no per-user attribution for [MCP][mcp] calls.
  - If you need per-user audit, front Caddy with a proxy that maps users to tokens.

### Secrets handling

- `.env` is gitignored (copy from `.env.example`) and holds no secrets (ports, tags, model names, tunables) — except `OLLAMA_BASE_URL`, which is deployment-specific.
- `.env.local` is gitignored (`chmod 0600`) and holds:
  - `WEBUI_SECRET_KEY` (generated by `make bootstrap`).
  - `GRAPHITI_API_TOKEN` (generated by `make bootstrap`, printed to the terminal).
  - `OPENWEBUI_ADMIN_API_KEY` and `OPENWEBUI_USER_API_KEY` (generated by `make api-keys`).
  - `OPENWEBUI_USER` / `OPENWEBUI_USER_PASSWORD` (the agent account; password generated by `make api-keys`).
- Secrets are injected into containers via `env_file`.
- `make start` rejects missing/empty secrets before `up -d`.
- `make preflight` also checks both secrets are set.
- Raw `docker compose up` does NOT fail-fast on empty secrets — use `make start`.
- Never commit a fixed secret. If a secret leaks, rotate it and `make restart`.

### Neo4j

- Auth is on (`NEO4J_AUTH=neo4j/password`).
- Not exposed to the host.
- For any non-local deployment, set a stronger `NEO4J_PASSWORD` in `.env`.

### Open WebUI feature lockdown

Defaults locked in `.env` to reduce exposure:

- Tools and Skills: no workspace access and no importing.
  - Users cannot upload/run arbitrary Python functions.
  - `USER_PERMISSIONS_WORKSPACE_TOOLS_*` and `USER_PERMISSIONS_WORKSPACE_SKILLS_*` are `False`.
- Direct tool/[MCP][mcp] servers: `USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS=False`.
- Web search: `ENABLE_WEB_SEARCH=False` and `USER_PERMISSIONS_FEATURES_WEB_SEARCH=False`.
- OpenAI passthrough proxy: `ENABLE_OPENAI_API=False`.
  - [Open WebUI][open-webui] talks to [Ollama][ollama] only; no external OpenAI-compatible upstream.
- Community sharing: `ENABLE_COMMUNITY_SHARING=False`.
- Evaluation arenas: `ENABLE_EVALUATION_ARENA_MODELS=False`.

Caveats:

- These are *default* user permissions for a fresh install.
- An admin can still grant Tools/Skills access to a user or group via the UI.
- `ENABLE_OPENAI_API=False` does NOT affect the agent REST API (`/api/chat/completions` etc.).
  - That flag only controls the external OpenAI *upstream* model source, not Open WebUI's own API.

### Phone-home / outbound hardening

No host mods; all of this is compose + `.env`. Verified against the upstream
code via DeepWiki.

| Service | Default outbound | What it sends | Disabled by |
|---|---|---|---|
| [Graphiti][graphiti] | `us.i.posthog.com` | anonymous init telemetry | `GRAPHITI_TELEMETRY_ENABLED=false` (compose.yml) |
| [Open WebUI][open-webui] | `api.github.com` | release update check | `ENABLE_VERSION_UPDATE_CHECK=False` |
| [Open WebUI][open-webui] | `openwebui.com` | favicon fetch (browser `<link>`) | `WEBUI_FAVICON_URL=/static/favicon.png` + mounted `./docs/xgensilicon.{png,ico}` |
| [Open WebUI][open-webui] | `localhost:4317` | OpenTelemetry exporter (off by default) | `ENABLE_OTEL=False` |

Notes:

- The favicon is bind-mounted over the Open WebUI build **source**
  (`/app/build/static/favicon.{png,ico}`), not the served path. Open WebUI re-syncs
  `FRONTEND_BUILD_DIR/static/*` into `STATIC_DIR` on every start, so mounting the
  source lets that sync copy our icon in with no log noise. The served paths are
  `/static/favicon.png` (modern browsers) and `/static/favicon.ico` (legacy).
- [HuggingFace][huggingface] (`huggingface.co`) is left **reachable**. It is a
  functional tokenizer/embedder download (tiktoken `cl100k_base`), not telemetry.
  Block it only for a full airgap (set `HF_HUB_OFFLINE=1` and test token counting).
- This is an **allowlist of known defaults**, not a firewall. To also catch
  future/unknown outbound domains, add a DNS sinkhole (`dns: ["0.0.0.0"]`) to the
  `openwebui` and `graphiti-mcp` services and pin the Ollama host in `extra_hosts` — but
  that is out of scope for this minimal hardening.

### Container hardening

- No service runs `privileged`.
- All four services set `security_opt: no-new-privileges:true`.
- [Caddy][caddy] gateway and graphiti-mcp also set `cap_drop: ALL` (no host-owned bind-mount writes).
- [Neo4j][neo4j] and [Open WebUI][open-webui] keep default capabilities:
  - Their entrypoints need `CAP_CHOWN` / `DAC_OVERRIDE` to write to the host-owned `./data` bind mounts.
  - Dropping all caps would break startup.
- To harden those two further:
  - `chown` their `./data` subdirs to the container user's UID.
  - Add `cap_drop: ALL` to the service.
  - Validate with `make start` first.

### Dev-mode docs

- `ENV=dev` exposes `/docs` (Swagger UI) and `/openapi.json` (schema) without auth.
- Both are read-only: no state mutation, no credentials exposed.
- If `:3000` is reachable from an untrusted network:
  - Put it behind a reverse proxy with TLS, or
  - Limit the port with a firewall to trusted LAN/VPN.

### Hardening recommendations

- Front `:3000` and `:8000` with a TLS reverse proxy for remote access.
- Use firewall rules to limit both ports to trusted networks or a VPN.
- Rotate `GRAPHITI_API_TOKEN` and [Open WebUI][open-webui] API keys on a schedule.
- Protect `./data`:
  - Holds `webui.db` (user credential hashes), uploaded documents, and the [Neo4j][neo4j] graph.
  - Restrict file permissions.
  - When moving to RAID, ensure the RAID volume keeps restrictive permissions.
- Keep image tags pinned (as in `.env`) and pull patches with `make pull`.

## Agents

Two interfaces for agents:

- **REST** — [Open WebUI][open-webui] `/api/*` with a per-user API key (Bearer). Chat, RAG, file and knowledge management.
- **[MCP][mcp]** — Graphiti at `http://<host>:8000/mcp` with `Authorization: Bearer <GRAPHITI_API_TOKEN>`. Memory and graph tools.

Replace `<host>` with the Docker host name/IP, or `localhost` if the client runs on the Docker host.

### Open WebUI REST

- Base path: `/api`.
- Auth: `Authorization: Bearer <OWUI API key>`.
- Two keys provisioned by `make api-keys` (see [API keys & agent access](#api-keys--agent-access)):
  - `OPENWEBUI_USER_API_KEY` (read-scoped, non-admin) — use this for agents.
  - `OPENWEBUI_ADMIN_API_KEY` (full admin) — admin tooling only.
  - Or sign in to get a JWT: `POST /api/v1/auths/signin` returns `token`; use it as the Bearer (same effect as an API key for that user).
- Chat (OpenAI-compatible) with [`gemma4:12b`][gemma4]:
  ```
  curl -s http://localhost:3000/api/chat/completions \
    -H "Authorization: Bearer <OWUI API key>" \
    -H "Content-Type: application/json" \
    -d '{"model":"gemma4:12b","stream":false,"messages":[{"role":"user","content":"Say hello."}]}'
  ```
- Upload a file (returns an id):
  ```
  curl -s http://localhost:3000/api/v1/files/ \
    -H "Authorization: Bearer <OWUI API key>" \
    -F 'file=@./note.txt'
  ```
- Bind the file to a knowledge collection:
  ```
  curl -s -X POST http://localhost:3000/api/v1/knowledge/<kb-id>/file/add \
    -H "Authorization: Bearer <OWUI API key>" \
    -H "Content-Type: application/json" \
    -d '{"file_id":"<file-id>"}'
  ```
- `kb` skill (read-scoped agent): a zero-dependency CLI wrapper (`scripts/owui.py`) that handles auth and flattens the Chroma nested-array search response. Reads `OPENWEBUI_HOST_PORT` + `OPENWEBUI_USER_API_KEY` + `OPENWEBUI_MODEL` from `.env` / `.env.local` via `--env-file`.
- Best way to trigger the skill in Claude:

  | Trigger | Example phrasing | Deterministic? |
  |---|---|---|
  | Slash command | `/kb` then the request | yes |
  | Natural — search | "search the KB for X" / "search the knowledge base for X" | by description match |
  | Natural — RAG chat | "ask the KB: \<question\>" / "ask the knowledge base: \<question\>" | by description match |
  | Natural — list | "list my KBs" / "list my knowledge bases" | by description match |

  The slash command is the most reliable; natural phrasing triggers automatically when it matches the skill description. Both "KB" and "knowledge base" phrasings are recognized.

### Graphiti MCP (HTTP)

- Endpoint: `http://<host>:8000/mcp` (no trailing slash; `/mcp/` 307-redirects to `/mcp`).
- Required headers: `Authorization: Bearer <GRAPHITI_API_TOKEN>` and `Accept: application/json, text/event-stream`.
- Without the token: `401`.
- Manual check — `initialize` (capture `Mcp-Session-Id` from the response headers):
  ```
  curl -s -D - -X POST http://localhost:8000/mcp \
    -H 'Authorization: Bearer <GRAPHITI_API_TOKEN>' \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
         "params":{"protocolVersion":"2025-03-26","capabilities":{},
                   "clientInfo":{"name":"verify","version":"1"}}}'
  ```
- Then send `notifications/initialized` (HTTP 202) and `tools/list`, both with the `Mcp-Session-Id` header.
- Tools: `add_memory`, `search_nodes`, `search_memory_facts`, `get_episodes`, `get_status`, `clear_graph`, plus delete/get helpers.

### Install in MCP clients

Export the token first (from `.env.local`; printed by `make bootstrap`):
```
export GRAPHITI_API_TOKEN=<token>
```

#### Claude Code / Claude Desktop

- CLI:
  ```
  claude mcp add --transport http graphiti http://<host>:8000/mcp \
    --header "Authorization: Bearer ${GRAPHITI_API_TOKEN}"
  ```
- Or JSON (`~/.claude.json` or project `.mcp.json`); `${VAR}` expands:
  ```json
  {
    "mcpServers": {
      "graphiti": {
        "type": "http",
        "url": "http://<host>:8000/mcp",
        "headers": { "Authorization": "Bearer ${GRAPHITI_API_TOKEN}" }
      }
    }
  }
  ```
- Verify: `claude mcp list` (look for `Connected`), or `/mcp` in a session.

#### Codex CLI

- `~/.codex/config.toml` (transport is implicit: `url` => streamable HTTP):
  ```toml
  [mcp_servers.graphiti]
  url = "http://<host>:8000/mcp"
  bearer_token_env_var = "GRAPHITI_API_TOKEN"
  startup_timeout_sec = 20
  ```
- Or CLI:
  ```
  codex mcp add graphiti --url http://<host>:8000/mcp --bearer-token-env-var GRAPHITI_API_TOKEN
  ```
- `bearer_token_env_var` sends `Authorization: Bearer <value of GRAPHITI_API_TOKEN>`.

#### Opencode

- `opencode.json` (project or `~/.config/opencode/opencode.json`); `{env:VAR}` interpolates:
  ```json
  {
    "$schema": "https://opencode.ai/config.json",
    "mcp": {
      "graphiti": {
        "type": "remote",
        "url": "http://<host>:8000/mcp",
        "enabled": true,
        "oauth": false,
        "headers": { "Authorization": "Bearer {env:GRAPHITI_API_TOKEN}" }
      }
    }
  }
  ```
- `oauth: false` stops Opencode from trying OAuth auto-detection.

### API keys & agent access

`make api-keys` provisions two REST API keys into `.env.local` (run after `make start` and admin signup):

| Variable | Account | Access | Hand to agents? |
|---|---|---|---|
| `OPENWEBUI_ADMIN_API_KEY` | `admin@local.test` (admin) | Full — bypasses access control | **No** |
| `OPENWEBUI_USER_API_KEY` | `agent@local.test` (user) | Read-scoped — sees KBs via their `*` read grants; cannot write to or delete the admin's KBs | **Yes** |

- The agent user is a dedicated non-admin account created by `make api-keys`. It has the same **read** scope as the admin only where KBs grant `*` (public read); it cannot write to KBs it does not own.
- Give agents the `OPENWEBUI_USER_API_KEY`. An agent cannot damage indexed documents: binding a file (`/api/v1/knowledge/{id}/file/add`), removing files, and deleting a KB are denied for KBs the agent does not own.
- `make api-keys` also grants `*` read on the chat model (`MODEL_NAME`) so the agent user can do RAG chat (`/api/chat/completions`). Without it, a non-admin user sees 0 models and chat returns `Model not found`.
- Prerequisites flipped by `make api-keys` (and set in `.env` for fresh first boots): `ENABLE_API_KEYS=true` and `USER_PERMISSIONS_FEATURES_API_KEYS=true`.
- Idempotent; `FORCE=1 make api-keys` rotates (replaces) the keys. The admin API key is also persisted in `webui.db`; rotating via this script invalidates any prior key.
- This is mechanism A (read-scoped non-admin user). For a hard API-layer read-only lock on every key, see the `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS` / `API_KEYS_ALLOWED_ENDPOINTS` admin config (Settings -> Admin -> API Key Endpoint Restrictions) — note it is global and also restricts the admin key.

### RAG governance

- A user's uploaded files and knowledge bases are private to that user by default.
- The KB owner (with the `sharing.knowledge` permission) or an admin grants a KB to user groups: Workspace -> Knowledge.
- An agent using a user's API key inherits that user's permissions: the user's own files + KBs shared with the user's groups. It cannot see other users' private docs.
- An admin API key bypasses access control. Give agents a dedicated low-priv user's key, not an admin key.
- To let an agent RAG a curated doc set: create a KB -> add docs -> grant it to a group -> put the agent's user in that group -> pass the KB in the `files` field of `/api/chat/completions` as `{"type":"collection","id":"<kb-id>"}`. A top-level `knowledge` field is ignored, and `metadata.knowledge` is discarded server-side — only `files` grounds.
- `make rag-config` sets a **strict-grounding RAG template** in Open WebUI (admin config, persisted in `webui.db`): answer only from the retrieved context; refuse with "The indexed documents do not contain this information." when the answer is absent; do not use outside knowledge or invent names/artifacts. The default template lets the model fall back to its own knowledge, which makes ~12B models confabulate (wrong vendor, invented file names). Re-run after any DB reset/rebuild. Grounding (chunk injection) is the caller's job (`files` field); this template governs what the model does with the chunks.

## Persistent data and moving to RAID

- All state is under `./data` (bind mounts, not named volumes).
- To move it to RAID:
  ```
  make stop
  mv ./data /mnt/RAID/kb/data
  ln -s /mnt/RAID/kb/data ./data
  make start
  ```
- `DATA_ROOT=./data` resolves through the symlink, so no `.env` edit is needed.

## Tests

System integration tests in `tests/` exercise the running stack over HTTP. They
need the stack up (`make start && make health`) and two extra keys in `.env.local`
(see `.env.local.example`):

- `OPENWEBUI_TEST_USER` / `OPENWEBUI_TEST_PASSWORD` — an existing [Open WebUI][open-webui]
  user (e.g. the admin). `test_03` signs in to get a JWT.

`GRAPHITI_API_TOKEN` (already in `.env.local`) authenticates the MCP tests.

Run the suite:

```
make test
```

| Script | Checks | Auth |
|---|---|---|
| `tests/test_01_health.sh` | `/health` endpoints, dev-mode `/openapi.json`, Neo4j not published, MCP gate rejects no-token | none |
| `tests/test_02_mcp.sh` | MCP `initialize` + session, `notifications/initialized`, `tools/list` (9 tools), `get_episodes` (graphiti → Neo4j read) | `GRAPHITI_API_TOKEN` |
| `tests/test_03_openwebui_rest.sh` | `signin` → JWT, chat completion → [Ollama][ollama] `MODEL_NAME` | `OPENWEBUI_TEST_USER/PASSWORD` |
| `tests/test_04_openwebui_rag.sh` | RAG embedding URL reachable from container; upload → embed → bind → `/api/v1/retrieval/query/collection` returns the indexed doc | `OPENWEBUI_TEST_USER/PASSWORD` |
| `tests/test_05_openwebui_user_readonly.sh` | Agent (`OPENWEBUI_USER_API_KEY`) is role=user; reads + searches a `*`-granted KB (`write_access=False`); denied `file/add` and `delete`; admin key contrast has write access; RAG chat grounded via `files:[{type:collection,id}]` — unique marker present in the answer (catches a `knowledge`-field regression) | `OPENWEBUI_ADMIN_API_KEY`, `OPENWEBUI_USER_API_KEY` |

Notes:

- The tests are read-only except `test_03` (one stateless chat completion), `test_04` (creates a KB + file, then deletes both on exit), and `test_05` (admin creates a temp KB + file + `*` read grant, then deletes all three on exit).
- Each script sources `tests/lib.sh` (env loader, pass/fail counters, stack-up guard) and exits non-zero on any failure.
- `make test` runs all scripts and exits non-zero if any fail.

## Make targets

| target | action |
|---|---|
| `help` | show targets (default) |
| `bootstrap` | create `.env.local` (generate `WEBUI_SECRET_KEY` + `GRAPHITI_API_TOKEN`) and `./data` dirs |
| `preflight` | read-only checks: docker, secrets set, Ollama, models |
| `pull` | pull Docker images |
| `pull-models` | pull Ollama models (`MODEL_NAME` + `nomic-embed-text`) on the host |
| `start` | `docker compose up -d` (rejects missing/empty secrets first) |
| `stop` | `docker compose stop` (keeps containers and data) |
| `restart` | stop then start |
| `logs` | tail logs |
| `ps` | container status (with health) |
| `config` | render effective compose config (secrets redacted) |
| `health` | probe graphiti and Open WebUI `/health` |
| `test` | run system integration tests against the running stack |
| `rag-config` | set the strict-grounding RAG template in Open WebUI (run after `make api-keys`; re-run after a DB reset) |
| `shell-owui` / `shell-neo4j` / `shell-graphiti` / `shell-caddy` | exec a shell |
| `clear` | `down --remove-orphans`; KEEPS `./data` and `.env.local` |
| `clear-all` | `down --volumes` + delete `./data` + delete `.env.local` |

- `clear` preserves all state (clean recreate).
- `clear-all` wipes data and the generated secret.
- `clear-all` keeps `.env`, `graphiti/config.yaml`, and `caddy/Caddyfile`.

## Notes

- Embedding dimensions must be 768 for [`nomic-embed-text`][nomic-embed-text].
  - Change `EMBEDDER_MODEL` and `EMBEDDER_DIMENSIONS` together if you swap models.
- [Graphiti][graphiti] uses `json_object` structured output ([Ollama][ollama] does not support `json_schema`).
- `GRAPHITI_API_TOKEN` is a shared secret. Rotate it in `.env.local`, then `make restart`.
- [Open WebUI][open-webui] exposes `/docs` and `/openapi.json` only in `ENV=dev`.
  - `ENV=dev` also raises log verbosity. Acceptable for an internal/LAN deployment.
- `SEMAPHORE_LIMIT=3` is conservative for one local [Ollama][ollama]. Raise it if the host Ollama has capacity.

[graphiti]: https://github.com/getzep/graphiti
[open-webui]: https://github.com/open-webui/open-webui
[neo4j]: https://neo4j.com/
[caddy]: https://caddyserver.com/
[ollama]: https://ollama.com/
[docker]: https://www.docker.com/
[docker-compose]: https://docs.docker.com/compose/
[mcp]: https://modelcontextprotocol.io/
[huggingface]: https://huggingface.co/
[nomic-embed-text]: https://huggingface.co/nomic-ai/nomic-embed-text
[gemma4]: https://ollama.com/library/gemma4
