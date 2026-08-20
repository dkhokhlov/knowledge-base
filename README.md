# KnowledgeBase

[![Graphiti](https://img.shields.io/badge/Graphiti-MCP-blue)](https://github.com/getzep/graphiti)
[![Open WebUI](https://img.shields.io/badge/Open_WebUI-RAG-orange)](https://github.com/open-webui/open-webui)
[![Neo4j](https://img.shields.io/badge/Neo4j-5.26-green)](https://neo4j.com/)
[![Caddy](https://img.shields.io/badge/Caddy-gateway-1f83c7)](https://caddyserver.com/)
[![Ollama](https://img.shields.io/badge/Ollama-host_LLM-000000)](https://ollama.com/)
[![embed](https://img.shields.io/badge/embed-nomic--embed--text-brightgreen)](https://huggingface.co/nomic-ai/nomic-embed-text)
[![LLM](https://img.shields.io/badge/LLM-gemma4_12b-blueviolet)](https://ollama.com/library/gemma4)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED)](https://docs.docker.com/compose/)
[![license](https://img.shields.io/badge/license-MIT-yellow)](./LICENSE)

Self-hosted, agent-first knowledge stack combining two complementary knowledge bases in one system. [Open WebUI][open-webui] provides a document knowledge base: ingest curated documents into access-controlled collections, vector-search them, and generate LLM-grounded RAG answers. [Graphiti MCP][graphiti] provides a temporal fact memory over [Neo4j][neo4j]: each episode is time-stamped, and extracted facts and edges are time-bound — a fact is true over a time window and is invalidated, not deleted, when superseded. This preserves history so the graph represents current truth, what was true when, and how knowledge changed — beyond static vector retrieval of fixed text chunks.

Documents provide grounded answers from a curated reference corpus. Fact memory replaces scattered, untrimmed README, notes, and tracker files across projects — where every context load pays a growing token tax and specific facts become hard to find — with one searchable temporal graph, so accumulated knowledge stays findable instead of bloating linearly with every addition. Agents use the stack through the stack-side `kb-gateway`, which authorizes per-account `KB_API_KEY` credentials with identity and role derived server-side, plus a thin zero-dependency CLI and the `/kb` skill. Humans can also use the Open WebUI web interface.

- **[Graphiti MCP][graphiti]** — temporal fact memory over [Neo4j][neo4j]; HTTP [MCP][mcp] server (internal only).
- **[Open WebUI][open-webui]** — document knowledge base with vector search, grounded RAG chat, and user/group access control; also the identity provider for the kb-gateway.
- **[Neo4j][neo4j]** — graph store for [Graphiti][graphiti] (internal only).
- **kb-gateway** — a custom component in this repo: stack-side authorization, per-account identity and role validation, Graphiti MCP bridge, live group discovery, and admin user provisioning (zero-dependency Python stdlib).
- **[Caddy][caddy]** — public edge that proxies agents to the kb-gateway.

[Ollama][ollama] supplies the chat LLM and [`nomic-embed-text`][nomic-embed-text] embeddings; it is reached via `OLLAMA_BASE_URL` and can run on the [Docker][docker] host or a remote/LAN host.

## Documentation map

README:

- [Architecture](#architecture)
- [Operating model](#operating-model)
- [Quick start](#quick-start)
- [Agent interfaces](#agent-interfaces)
- [Security](#security)
- [Repository layout](#repository-layout)
- [Notes](#notes)

Sub-documents:

- [docs/operations.md](docs/operations.md) — prerequisites, configuration (env vars), Ollama host service, persistent data / RAID, make targets, troubleshooting, full hardening reference.
- [docs/testing.md](docs/testing.md) — integration test suite + matrix.

## Architecture

```
Agent (any host)
  kb_gateway.py  (thin REST client; holds only KB_API_KEY + KB_GATEWAY_URL)
  |
  |  HTTP, Authorization: Bearer <KB_API_KEY>  (an Open Web UI per-account API key)
  v
Caddy :8000  (public edge; reverse_proxy kb-gateway:8010; /health ungated)
  |
  v
kb-gateway :8010  (zero-dependency Python stdlib)
  |  derives identity + role from KB_API_KEY via Open Web UI (tamper-proof)
  |  writes go to the caller's own personal group; reads span all groups (discovered from Neo4j)
  |  destructive ops require owning the target group or admin; admin creates users
  |
  +-- owui_net       --> Open WebUI :3000   (identity + user provisioning + RAG)
  +-- graph_internal --> graphiti-mcp :8000 (MCP handshake + tool calls)
  \-- graph_internal --> Neo4j :7474        (group discovery + delete guards)

User (browser) -- HTTP :3000 --> Open WebUI :3000   (chat UI + RAG + admin)

Open WebUI :3000 and graphiti-mcp both reach Ollama :11434 (host, via
host-gateway) for gemma4:12b (chat) and nomic-embed-text (embeddings).
```

- A user with a browser reaches [Open WebUI][open-webui] over HTTP (`:3000`) for the chat UI, RAG, and admin.
- An agent reaches the stack through **one** REST interface: `kb_gateway.py` → Caddy `:8000` → **kb-gateway** `:8010`. The agent holds only `KB_API_KEY` + `KB_GATEWAY_URL` (no Graphiti token, no repo files) — it works on any host.
- **kb-gateway** is the sole bridge to the graph. It resolves the caller's identity + role from `KB_API_KEY` via Open WebUI (tamper-proof), enforces ownership-bounded writes + owner/admin destructive gating, discovers all existing groups live from [Neo4j][neo4j], runs the [MCP][mcp] handshake to graphiti-mcp, and provisions new KB users for admins.
- **Network split**: `graph_internal` (neo4j + graphiti-mcp + kb-gateway), `edge` (caddy + kb-gateway), `owui_net` (kb-gateway + openwebui). graphiti-mcp and Neo4j are **internal-only** — no host ports, reachable only through the gateway.
- Only `:3000` ([Open WebUI][open-webui]) and `:8000` (Caddy → kb-gateway) bind to the host.
- [Ollama][ollama] is external on the Docker host (reached via host-gateway); not published by this stack. [Open WebUI][open-webui] (RAG + chat) and graphiti-mcp (LLM + embedder) both reach it.

## Operating model

The stack is **agent-facing**: the actor that calls the gateway is an **agent**. A **role** is `admin` or `user` (the Open Web UI role field). An **account** is an Open Web UI account that holds a role; `KB_API_KEY` is **per-account** (an Open Web UI API key).

- **Identity is tamper-proof.** The gateway resolves `(id, email, role)` from `KB_API_KEY` via Open Web UI `GET /api/v1/auths/`. The caller cannot set or influence it — there is no `KB_USER_ID` env var, no spoofable header.
- **Authorization = role + personal-group ownership**, both enforced on the stack (not bypassable by a modified CLI):
  - **Writes go to your own personal group.** `add` with no `--group` writes to `user:<email>`. `add --group G` is allowed only if `G` is your own personal group; any other group → `403`. There are **no shared write groups** — reads are how knowledge is shared across accounts.
  - **Reads span all groups that have data**, discovered live from [Neo4j][neo4j] (no roster file). `search`, `episodes`, `status`, `groups` are read-only for everyone.
  - **Destructive ops** (`forget`, `delete-edge`, `delete-episode`) require owning the target group or admin. Admin (`role=admin`) overrides ownership and is the only role that can create users.

### Exposed endpoints

| Endpoint | Auth | Use |
|---|---|---|
| `http://<host>:3000` | session (web UI) | document upload, chat, admin, users, knowledge bases |
| `http://<host>:3000/api/*` | `Authorization: Bearer <OWUI API key>` | REST for agents (chat, RAG, files, KBs) |
| `http://<host>:3000/docs` | none (read-only) | Swagger UI |
| `http://<host>:3000/openapi.json` | none (read-only) | OpenAPI schema |
| `http://<host>:8000/mem/*` | `Authorization: Bearer <KB_API_KEY>` | kb-gateway: memory (whoami, groups, add, search, episodes, status, forget, delete-edge, delete-episode) |
| `http://<host>:8000/admin/users` | `Authorization: Bearer <KB_API_KEY>` (admin) | kb-gateway: create a new KB user (returns temp password + `KB_API_KEY`) |
| `http://<host>:8000/health` | none (read-only) | health probe (Caddy → kb-gateway → OWUI) |

[Neo4j][neo4j] (`:7474`, `:7687`) and graphiti-mcp (`:8000` internal) are not published — reachable only through the kb-gateway over `graph_internal`.

## Quick start

Full prerequisites, configuration, and env vars are in [docs/operations.md](docs/operations.md). Core sequence:

1. **Bootstrap** the local secret file and data dirs:
   ```
   make bootstrap
   ```
   Generates `WEBUI_SECRET_KEY` into `.env.local` (`0600`), creates `./data/{neo4j/data,neo4j/logs,openwebui}`, and creates `.env` from `.env.example` if absent. Set `OLLAMA_BASE_URL` in `.env` to your Ollama host before `make start`.
2. Set `KB_GATEWAY_URL` for agent clients (in `.env` / `.env.local`): `http://localhost:8000` on the Docker host, or `https://<host>` / VPN for a remote agent (`KB_API_KEY` is a bearer — plain HTTP only on a trusted local interface).
3. **Pull models**, **preflight**, **start**, **verify**:
   ```
   make pull-models
   make preflight
   make start
   make health
   ```
4. Open `http://<your-host-ip>:3000` and register the first user (becomes admin). Set `OPENWEBUI_TEST_USER` / `OPENWEBUI_TEST_PASSWORD` in `.env.local` to this admin (used by `make test` and `make api-keys`).
5. **Provision API keys** (admin + read-scoped agent) into `.env.local`:
   ```
   make api-keys
   ```
   Idempotent; `FORCE=1` rotates. See [API keys](#api-keys).
6. **Set the strict-grounding RAG template**:
   ```
   make rag-config
   ```
   Re-run after a DB reset/rebuild or an `OLLAMA_BASE_URL` change.

Provisioning sequence: `make start` → (admin signs up in UI) → `make api-keys` → `make rag-config`.

7. (Optional) Close signup: set `ENABLE_SIGNUP=false` in `.env` and `make restart`.

## Agent interfaces

Two interfaces for agents, both keyed with `KB_API_KEY` (an Open Web UI per-account API key):

- **Open Web UI REST** (`:3000/api/*`) — chat, RAG, file and knowledge-base management. Direct to Open Web UI.
- **kb-gateway REST** (`:8000/mem/*`, `:8000/admin/users`) — Graphiti memory + graph tools and admin user provisioning. A thin CLI (`kb_gateway.py`) wraps it; the agent never speaks [MCP][mcp] directly (graphiti-mcp is internal-only).

Replace `<host>` with the Docker host name/IP, or `localhost` if the client runs on the Docker host.

### API keys

`make api-keys` provisions two REST API keys into `.env.local` (run after `make start` and admin signup):

| Variable | Account | Access | Hand to agents? |
|---|---|---|---|
| `OPENWEBUI_ADMIN_API_KEY` | `admin@local.test` (admin) | Full — bypasses access control | **No** |
| `OPENWEBUI_USER_API_KEY` | `agent@local.test` (user) | Read-scoped — sees KBs via their `*` read grants; cannot write to or delete the admin's KBs | **Yes** |

- Either is a valid `KB_API_KEY` for the kb-gateway (admin key → admin role; agent key → read-scoped). `KB_API_KEY` may also be a per-account key issued by the gateway (`POST /admin/users`). The CLI falls back to `OPENWEBUI_USER_API_KEY` if `KB_API_KEY` is unset. See [docs/operations.md](docs/operations.md#environment-variables) for storage details.
- `make api-keys` also grants `*` read on the chat model (`MODEL_NAME`) so the agent user can do RAG chat. Without it, a non-admin user sees 0 models and chat returns `Model not found`.
- Idempotent; `FORCE=1 make api-keys` rotates (replaces) the keys.
- For a hard API-layer read-only lock on every key: Settings -> Admin -> API Key Endpoint Restrictions (`ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS`) — global, also restricts the admin key.

### Open Web UI REST

- Base path: `/api`; auth `Authorization: Bearer <OWUI API key>` (or a JWT from `POST /api/v1/auths/signin`).
- OpenAPI schema: `GET /openapi.json`; interactive Swagger UI: `/api/docs` (`ENV=dev` only).
- `kb` skill (read-scoped agent): a zero-dependency CLI wrapper (`scripts/owui.py`) that handles auth and flattens the Chroma nested-array search response. Reads `OPENWEBUI_HOST_PORT` + `OPENWEBUI_USER_API_KEY` + `OPENWEBUI_MODEL` from `.env` / `.env.local` via `--env-file`.

  | Trigger | Example phrasing | Deterministic? |
  |---|---|---|
  | Slash command | `/kb` then the request | yes |
  | Natural — search | "search the KB for X" / "search the knowledge base for X" | by description match |
  | Natural — RAG chat | "ask the KB: \<question\>" / "ask the knowledge base: \<question\>" | by description match |
  | Natural — list | "list my KBs" / "list my knowledge bases" | by description match |

  The slash command is the most reliable; natural phrasing triggers automatically when it matches the skill description. Both "KB" and "knowledge base" phrasings are recognized.

### Graphiti memory (kb-gateway)

- Endpoint `http://<host>:8000` (Caddy → kb-gateway). All paths under `/mem/*` and `/admin/users` require `Authorization: Bearer <KB_API_KEY>`; `/health` is ungated. Identity + role are derived from the key (tamper-proof).
- Thin CLI (`scripts/kb_gateway.py`, zero-dependency): reads `KB_API_KEY` + `KB_GATEWAY_URL` from `.env` / `.env.local` via `--env-file`. Subcommands:
  ```
  python3 ~/.claude/skills/kb/scripts/kb_gateway.py --env-file .env --env-file .env.local whoami
  python3 ~/.claude/skills/kb/scripts/kb_gateway.py ... groups
  python3 ~/.claude/skills/kb/scripts/kb_gateway.py ... add "Project Atlas uses QPU scheduler" --name atlas
  python3 ~/.claude/skills/kb/scripts/kb_gateway.py ... search "atlas scheduler"
  python3 ~/.claude/skills/kb/scripts/kb_gateway.py ... episodes
  python3 ~/.claude/skills/kb/scripts/kb_gateway.py ... status
  python3 ~/.claude/skills/kb/scripts/kb_gateway.py ... forget user:<me>
  python3 ~/.claude/skills/kb/scripts/kb_gateway.py ... delete-edge <uuid>
  python3 ~/.claude/skills/kb/scripts/kb_gateway.py ... delete-episode <uuid>
  python3 ~/.claude/skills/kb/scripts/kb_gateway.py ... user-create --email alice@example.com --name Alice
  ```
- Authorization rules are in [Operating model](#operating-model): personal-only writes, read-all, owner/admin destructive. The `/kb` skill exposes these as natural-language triggers ("remember …", "what do we know about …", "forget …") plus the slash command.

### KB user provisioning (admin)

An admin tells an agent **"create a new KB user alice\@example.com named Alice"**; the agent runs `kb_gateway.py ... user-create --email alice@example.com --name Alice`.

- The gateway enforces `role=admin` **server-side** before any write. A non-admin `KB_API_KEY` → `403` (not merely a CLI check). OWUI down → `503`.
- Flow (all inside the gateway, one stateless request): generate a strong temp password → create the OWUI user (`POST /api/v1/auths/add`, admin key) → sign in as the new user → generate that user's `KB_API_KEY` with the new user's own JWT (`POST /api/v1/auths/api_key`) → verify the key via `GET /api/v1/auths/` resolves to the expected email + `role=user`.
- Returns to the admin **only**: `email`, `temp_password`, `kb_api_key`, `role`, `id`. The gateway is stateless — it **never persists** the password or key; they exist only in the one response. The agent must relay them to the requesting administrator and not store them.
- **Rollback**: if any step after user creation fails, the gateway deletes the partial user (admin `DELETE /api/v1/users/{id}`) and returns a clear error. It never reports success on partial provisioning. A duplicate email → deterministic `409` (no second account).
- Prerequisite: the deployed Open Web UI image must expose the provisioning endpoints. The gateway probes `/openapi.json` at startup and returns `501` from `/admin/users` if the image lacks them.

### RAG governance

- A user's uploaded files and knowledge bases are private to that user by default. The KB owner (with `sharing.knowledge`) or an admin grants a KB to user groups: Workspace -> Knowledge.
- An agent using a user's API key inherits that user's permissions: the user's own files + KBs shared with the user's groups. It cannot see other users' private docs. An admin key bypasses access control — give agents a dedicated low-priv user's key, not an admin key.
- To RAG a curated doc set: create a KB -> add docs -> grant it to a group -> put the agent's user in that group -> pass the KB in the `files` field of `/api/chat/completions` as `{"type":"collection","id":"<kb-id>"}`. A top-level `knowledge` field is ignored, and `metadata.knowledge` is discarded server-side — only `files` grounds.
- `make rag-config` sets a **strict-grounding RAG template** (admin config, persisted in `webui.db`): answer only from the retrieved context; refuse when the answer is absent; do not use outside knowledge or invent names/artifacts. The default template lets the model fall back to its own knowledge, which makes ~12B models confabulate. Re-run after any DB reset/rebuild. Grounding (chunk injection) is the caller's job (`files` field); this template governs what the model does with the chunks. It also syncs `rag.ollama.base_url` to `.env` `OLLAMA_BASE_URL` (which OWUI otherwise leaves stale after a host change; `make preflight` warns on drift).

## Security

The trust model in brief. For lockdown defaults, phone-home hardening, container caps, secrets handling, Neo4j auth, and dev-mode docs, see [docs/operations.md#hardening-reference](docs/operations.md#hardening-reference).

### Open WebUI (`:3000`)

- `WEBUI_AUTH=true`: every UI and REST call needs a session or a Bearer API key.
- First registered user becomes admin. Later signups get `DEFAULT_USER_ROLE=pending` and need admin approval. Close signup with `ENABLE_SIGNUP=false` + `make restart`.
- Knowledge bases are private by default; admins grant access to user groups (Workspace -> Knowledge). RBAC is additive: role + group membership.
- JWTs signed with `WEBUI_SECRET_KEY` (stored only in gitignored `.env.local`; `make start` rejects an empty/missing key).

### kb-gateway (`:8000` → `:8010`)

- graphiti-mcp and Neo4j have no agent-facing auth and are **internal-only** on `graph_internal`. The kb-gateway is the sole bridge; [Caddy][caddy] (`:8000`) proxies to it. graphiti-mcp has no native auth — the gateway is the gate.
- Every gateway endpoint (except `/health`) requires `Authorization: Bearer <KB_API_KEY>`. No key → `401`; bad key → `401`; Open WebUI unreachable → `503` (fail closed).
- `/health` is ungated on purpose: non-sensitive probe, carries no data or credentials. It also probes Open WebUI so `make health` catches an identity-broken stack rather than reporting healthy while auth is down.
- `KB_API_KEY` is a bearer — `KB_GATEWAY_URL` MUST be HTTPS or VPN/tunnel for any non-local agent. Plain HTTP is safe only on a trusted local interface.
- Rotate [Open WebUI][open-webui] API keys on a schedule (`FORCE=1 make api-keys`).

## Repository layout

| Path | Contents |
|---|---|
| `compose.yml` | services + networks (`graph_internal`, `edge`, `owui_net`); kb-gateway internal env |
| `gateway/` | kb-gateway: `app.py`, `authorize.py`, `mcp.py`, `neo4j.py`, `owui.py`, `Dockerfile` (zero-dependency stdlib, non-root) |
| `caddy/Caddyfile` | public edge; `reverse_proxy kb-gateway:8010` (no token block) |
| `graphiti/config.yaml` | Graphiti MCP config (LLM + embedder → Ollama `/v1`; `nomic-embed-text`, 768-dim; ro mount) |
| `scripts/` | `bootstrap.sh`, `api-keys.sh`, `preflight.sh`, `rag-config.sh` |
| `tests/` | `test_01`..`test_07` + `lib.sh` (see [docs/testing.md](docs/testing.md)) |
| `docs/` | `operations.md`, `testing.md`, favicon assets |
| `.env` / `.env.example` | tracked template — no secrets (ports, tags, models, tunables, `OLLAMA_BASE_URL`) |
| `.env.local` / `.env.local.example` | gitignored secrets + generated keys (`chmod 0600`) |
| `Makefile` | targets (see [docs/operations.md#make-targets](docs/operations.md#make-targets)) |
| `LICENSE` | MIT |

## Notes

- Embedding dimensions must be 768 for [`nomic-embed-text`][nomic-embed-text]. Change `EMBEDDER_MODEL` and `EMBEDDER_DIMENSIONS` together if you swap models.
- [Graphiti][graphiti] uses `json_object` structured output ([Ollama][ollama] does not support `json_schema`).
- There are no shared write groups. Each account writes to its own `user:<email>` group; reads span all groups, so cross-account knowledge is shared read-only.
- [Open WebUI][open-webui] exposes `/docs` and `/openapi.json` only in `ENV=dev` (also raises log verbosity — acceptable for an internal/LAN deployment).
- `SEMAPHORE_LIMIT=3` is conservative for one local [Ollama][ollama]. Raise it if the host Ollama has capacity.

[graphiti]: https://github.com/getzep/graphiti
[open-webui]: https://github.com/open-webui/open-webui
[neo4j]: https://neo4j.com/
[caddy]: https://caddyserver.com/
[ollama]: https://ollama.com/
[docker]: https://www.docker.com/
[mcp]: https://modelcontextprotocol.io/
[nomic-embed-text]: https://huggingface.co/nomic-ai/nomic-embed-text