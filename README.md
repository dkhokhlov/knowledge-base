# KnowledgeBase

[![Graphiti](https://img.shields.io/badge/Graphiti-REST-blue)](https://github.com/getzep/graphiti)
[![Open WebUI](https://img.shields.io/badge/Open_WebUI-RAG-orange)](https://github.com/open-webui/open-webui)
[![Neo4j](https://img.shields.io/badge/Neo4j-5.26-green)](https://neo4j.com/)
[![Caddy](https://img.shields.io/badge/Caddy-gateway-1f83c7)](https://caddyserver.com/)
[![Ollama](https://img.shields.io/badge/Ollama-host_LLM-000000)](https://ollama.com/)
[![embed](https://img.shields.io/badge/embed-nomic--embed--text-brightgreen)](https://huggingface.co/nomic-ai/nomic-embed-text)
[![LLM](https://img.shields.io/badge/LLM-qwen2.5_14b-blueviolet)](https://ollama.com/library/qwen2.5)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED)](https://docs.docker.com/compose/)
[![license](https://img.shields.io/badge/license-MIT-yellow)](./LICENSE)

Self-hosted, agent-first knowledge stack combining two complementary knowledge bases in one system. [Open WebUI][open-webui] provides a document knowledge base: ingest curated documents into access-controlled collections; query by RAG (LLM-grounded answer) or precise text / raw chunk retrieval (no LLM). [Graphiti][graphiti] provides a temporal fact memory over [Neo4j][neo4j]: each episode is time-stamped, and extracted facts and edges are time-bound — a fact is true over a time window and is invalidated, not deleted, when superseded. This preserves history so the graph represents current truth, what was true when, and how knowledge changed — beyond static vector retrieval of fixed text chunks.

Documents provide grounded answers from a curated reference corpus. Fact memory replaces scattered, untrimmed README, notes, and tracker files across projects — where every context load pays a growing token tax and specific facts become hard to find — with one searchable temporal graph, so accumulated knowledge stays findable instead of bloating linearly with every addition. Agents use the stack through the stack-side `kb-gateway`, which authorizes per-account `KB_API_KEY` credentials with identity and role derived server-side, plus a thin zero-dependency CLI and the `/kb` skill. Humans can also use the Open WebUI web interface.

- **[Graphiti][graphiti]** — temporal fact memory over [Neo4j][neo4j]; reached via an internal REST server.
- **[Open WebUI][open-webui]** — document knowledge base with vector search, grounded RAG chat, and user/group access control; also the identity provider for the kb-gateway.
- **[Neo4j][neo4j]** — graph store for [Graphiti][graphiti] (internal only).
- **kb-gateway** — a custom component in this repo: stack-side authorization, per-account identity and role validation, Graphiti REST bridge, live group discovery, and admin user provisioning (zero-dependency Python stdlib).
- **[Caddy][caddy]** — the single public edge (`KB_HOST`): fronts Open WebUI at the root and proxies `/memory/*`, `POST /admin/users`, `/health` to the kb-gateway.

[Ollama][ollama] supplies the chat LLM and [`nomic-embed-text`][nomic-embed-text] embeddings; it is reached via `OLLAMA_HOST` (Ollama's native client env var) and can run on the [Docker][docker] host or a remote/LAN host.

## Documentation map

README:

- [Architecture](#architecture)
- [Operating model](#operating-model)
- [Quick start](#quick-start)
- [Performance](#performance)
- [Security](#security)
- [Repository layout](#repository-layout)
- [Notes](#notes)

Sub-documents:

- [docs/operations.md](docs/operations.md) — prerequisites, configuration (env vars), Ollama host service, persistent data / RAID, make targets, troubleshooting, full hardening reference.
- [docs/testing.md](docs/testing.md) — integration test suite + matrix.
- [docs/agents.md](docs/agents.md) — agent integration per tool (Claude Code, Codex, OpenCode, Pi): install the `kb` skill, set `KB_HOST` + `KB_API_KEY`, example flows.

## Architecture

```
Agent (any host)                              User (browser)
  |  kb_gateway.py / owui.py                    |
  |  holds KB_API_KEY only                      |
  v                                             v
             KB_HOST  (http://<host>:3000)
                    |
                    v
         Caddy :3000  (single public edge; internal :3000 == host :3000, no port translation)
            |
            +-- /memory/*            --> kb-gateway :8010
            +-- POST /admin/users     --> kb-gateway :8010   (GET falls through to the OWUI SPA)
            +-- /health               --> kb-gateway :8010   (aggregated; probes OWUI)
            +-- /* (catch-all)        --> openwebui :8080     (OWUI landing + /api/* + SPA)

         kb-gateway :8010  (zero-dep stdlib; identity + role from KB_API_KEY via OWUI, tamper-proof)
            |
            +-- owui_net       --> Open WebUI :8080   (identity + user provisioning + RAG)
            +-- graph_internal --> graphiti :8000     (REST; bootstrap.py injects Ollama-compatible clients)
            +-- graph_internal --> Neo4j :7474/:7687 (group discovery + delete guards)

  Open WebUI :8080 + graphiti :8000 --> Ollama :11434 (Docker host, via host-gateway)
                                             qwen2.5:14b (chat + extraction, non-reasoning)  |  nomic-embed-text (embed)

  Published host port: only KB_HOST_PORT (default 3000) -> Caddy :3000.
  Internal-only: Neo4j + graphiti (graph_internal); Open WebUI (owui_net, fronted by Caddy).
```

- A user with a browser and an agent both reach the stack through **one** URL, **`KB_HOST`** (default `http://localhost:3000`): [Caddy][caddy] fronts [Open WebUI][open-webui] at the root and the **kb-gateway** under `/memory/*`, `POST /admin/users`, and `/health`. One port, one var for agents (mirrors `OLLAMA_HOST`).
- An agent holds only `KB_API_KEY` + `KB_HOST` (no Graphiti token, no repo files) — it works on any host. Its CLI (`kb_gateway.py` / `owui.py`) hits `KB_HOST`: OWUI REST at `/api/*`, kb-gateway memory at `/memory/*`.
- **kb-gateway** is the sole bridge to the graph. It resolves the caller's identity + role from `KB_API_KEY` via Open WebUI (tamper-proof), enforces ownership-bounded writes + owner/admin destructive gating, discovers all existing groups live from [Neo4j][neo4j], calls the internal [Graphiti][graphiti] REST server, and provisions new KB users for admins.
- **Graphiti client injection.** The `ghcr.io/dkhokhlov/graphiti-rest` server defaults to `OpenAIClient` (OpenAI Responses API), which [Ollama][ollama] cannot satisfy — entity/fact extraction silently stores nothing. `graphiti/bootstrap.py` is mounted into the container and run as the command; it overrides the FastAPI dependency to inject the stock `OpenAIGenericClient` (graphiti_core >= 0.29 defaults to **`json_schema` structured outputs**, which Ollama enforces server-side; set at `temperature=0`) + `OpenAIEmbedder` (`nomic-embed-text`, 768-dim), so extraction works with Ollama. There is no config switch for this; the injection is required. See `graphiti/bootstrap.py`.
- **Network split**: `graph_internal` (neo4j + graphiti + kb-gateway), `edge` (caddy + kb-gateway), `owui_net` (caddy + kb-gateway + openwebui). graphiti and Neo4j are **internal-only** — no host ports, reachable only through the gateway. Open WebUI is internal-only too (fronted by Caddy).
- Only `KB_HOST_PORT` (default `3000`) → Caddy `:3000` binds to the host. Caddy's `depends_on` uses `service_started` (not `service_healthy`) so the OWUI root stays reachable even if the gateway is broken.
- [Ollama][ollama] is external on the Docker host (reached via host-gateway); not published by this stack. [Open WebUI][open-webui] (RAG + chat) and graphiti (extraction LLM + embedder) both reach it.
- **TLS**: the Caddyfile is plain HTTP on the port. For a non-local `KB_HOST` you MUST either front Caddy with an upstream TLS reverse proxy, or switch the `:3000` site block to a hostname block so Caddy auto-terminates TLS and publishes `:443`. For the local MVP (`http://localhost:3000`) no TLS is needed.

## Operating model

The stack is **agent-facing**: the actor that calls the gateway is an **agent**. A **role** is `admin` or `user` (the Open Web UI role field). An **account** is an Open Web UI account that holds a role; `KB_API_KEY` is **per-account** (an Open Web UI API key).

- **Identity is tamper-proof.** The gateway resolves `(id, email, role)` from `KB_API_KEY` via Open Web UI `GET /api/v1/auths/`. The caller cannot set or influence it — there is no `KB_USER_ID` env var, no spoofable header.
- **Authorization = role + personal-group ownership**, both enforced on the stack (not bypassable by a modified CLI):
  - **Writes go to your own personal group.** `add` with no `--group` writes to `user:<email>`. `add --group G` is allowed only if `G` is your own personal group; any other group → `403`. There are **no shared write groups** — reads are how knowledge is shared across accounts. Graphiti stores the personal group as a charset-safe id (`user-<sanitized-email>`, e.g. `user-agent-local-test`); `forget` accepts the `user:<email>` form too.
  - **Reads span all groups that have data**, discovered live from [Neo4j][neo4j] (no roster file). `search`, `episodes`, `status`, `groups` are read-only for everyone.
  - **Destructive ops** (`forget`, `delete-edge`, `delete-episode`) require owning the target group or admin. Admin (`role=admin`) overrides ownership and is the only role that can create users.

### Exposed endpoints

All endpoints are on one URL, **`KB_HOST`** (`http://<host>:3000` by default). Caddy routes by path.

| Endpoint | Auth | Use |
|---|---|---|
| `KB_HOST/` | session (web UI) | Open WebUI: document upload, chat, admin, users, knowledge bases |
| `KB_HOST/api/*` | `Authorization: Bearer <OWUI API key>` | OWUI REST for agents (chat, RAG, files, KBs) |
| `KB_HOST/docs` | none (read-only) | Swagger UI (OWUI; `ENV=dev`) |
| `KB_HOST/openapi.json` | none (read-only) | OpenAPI schema (OWUI) |
| `KB_HOST/memory/*` | `Authorization: Bearer <KB_API_KEY>` | kb-gateway: memory (whoami, groups, add, search, episodes, status, forget, delete-edge, delete-episode) |
| `KB_HOST/admin/users` | `Authorization: Bearer <KB_API_KEY>` (admin, POST) | kb-gateway: create a new KB user (returns temp password + `KB_API_KEY`); GET falls through to the OWUI SPA |
| `KB_HOST/health` | none (read-only) | health probe (Caddy → kb-gateway → OWUI, aggregated) |

`KB_HOST` is read from `.env` (default `http://localhost:3000`), or synthesized from `KB_HOST_PORT`. [Neo4j][neo4j] (`:7474`, `:7687`) and graphiti (`:8000` internal) are not published — reachable only through the kb-gateway over `graph_internal`.

### KB user provisioning (admin)

An admin tells an agent **"create a new KB user alice\@example.com named Alice"**; the agent runs `kb_gateway.py ... user-create --email alice@example.com --name Alice`.

- The gateway enforces `role=admin` **server-side** before any write. A non-admin key → `403` (not merely a CLI check). OWUI down → `503`.
- Flow (all inside the gateway, one stateless request): generate a strong temp password → create the OWUI user (`POST /api/v1/auths/add`, admin key) → sign in as the new user → generate that user's `KB_API_KEY` with the new user's own JWT (`POST /api/v1/auths/api_key`) → verify the key via `GET /api/v1/auths/` resolves to the expected email + `role=user`.
- Returns to the admin **only**: `email`, `temp_password`, `kb_api_key`, `role`, `id`. The gateway is stateless — it **never persists** the password or key; they exist only in the one response. The agent must relay them to the requesting administrator and not store them.
- **Rollback**: if any step after user creation fails, the gateway deletes the partial user (admin `DELETE /api/v1/users/{id}`) and returns a clear error. It never reports success on partial provisioning. A duplicate email → deterministic `409` (no second account).
- Prerequisite: the deployed Open Web UI image must expose the provisioning endpoints. The gateway probes `/openapi.json` at startup and returns `501` from `/admin/users` if the image lacks them.

### RAG governance

- A user's uploaded files and knowledge bases are private to that user by default. The KB owner (with `sharing.knowledge`) or an admin grants a KB to user groups: Workspace -> Knowledge.
- An agent using a user's API key inherits that user's permissions: the user's own files + KBs shared with the user's groups. It cannot see other users' private docs. An admin key bypasses access control — give agents a dedicated low-priv user's key, not an admin key.
- To RAG a curated doc set: create a KB -> add docs -> grant it to a group -> put the agent's user in that group -> pass the KB in the `files` field of `/api/chat/completions` as `{"type":"collection","id":"<kb-id>"}`. A top-level `knowledge` field is ignored, and `metadata.knowledge` is discarded server-side — only `files` grounds.
- `make rag-config` sets a **strict-grounding RAG template** (admin config, persisted in `webui.db`): answer only from the retrieved context; refuse when the answer is absent; do not use outside knowledge or invent names/artifacts. The default template lets the model fall back to its own knowledge, which makes ~12B models confabulate. Re-run after any DB reset/rebuild. Grounding (chunk injection) is the caller's job (`files` field); this template governs what the model does with the chunks. It also syncs `rag.ollama.base_url` to `OLLAMA_HOST` (which OWUI otherwise leaves stale after a host change; `make preflight` warns on drift).

For per-tool agent integration (skill install, CLI examples), see [docs/agents.md](docs/agents.md).

## Quick start

Full prerequisites, configuration, and env vars are in [docs/operations.md](docs/operations.md). Core sequence:

**Minimum env vars to set** — everything else has a working default or is auto-generated:

- `.env` → `OLLAMA_HOST`: the only hard blocker. `make start` refuses the `<ollama-host>` placeholder. Set your Ollama URL (`http://host.docker.internal:11434` if Ollama runs on the Docker host) in shell env or `.env` (shell overrides `.env`).
- `.env` → `KB_HOST`: the single public URL agents/clients point at (default `http://localhost:3000`). `KB_HOST_PORT` (default `3000`) is the only host-published port. Change `KB_HOST` for remote agents (HTTPS/VPN).

A generated JWT signing key (`make bootstrap`), the API keys (`make api-keys`), and Neo4j auth are generated or default locally. Test-only credentials for `make test` are described in [docs/testing.md](docs/testing.md).

1. **Bootstrap** the local secret file and data dirs:
   ```
   make bootstrap
   ```
   Generates the JWT signing key into `.env.local` (`0600`), creates `./data/{neo4j/data,neo4j/logs,openwebui}`, and creates `.env` from `.env.example` if absent. Set `OLLAMA_HOST` (shell env or `.env`) to your Ollama URL before `make start`.
2. Set `KB_HOST` for agent clients (in `.env`): `http://localhost:3000` on the Docker host, or `https://<host>` / VPN for a remote agent (`KB_API_KEY` is a bearer — plain HTTP only on a trusted local interface).
3. **Pull models**, **preflight**, **start**, **verify**:
   ```
   make pull-models
   make preflight
   make start
   make health
   ```
4. Open `KB_HOST` (`http://<your-host-ip>:3000`) and register the first user (becomes admin). Record this admin as the test user in `.env.local` (see [docs/testing.md](docs/testing.md); used by `make test` and `make api-keys`).
5. **Provision API keys** (admin + read-scoped agent) into `.env.local`:
   ```
   make api-keys
   ```
   Idempotent; `FORCE=1` rotates. See [docs/operations.md](docs/operations.md#environment-variables).
6. **Set the strict-grounding RAG template**:
   ```
   make rag-config
   ```
   Re-run after a DB reset/rebuild or an `OLLAMA_HOST` change.

Provisioning sequence: `make start` → (admin signs up in UI) → `make api-keys` → `make rag-config`.

7. (Optional) Close signup: set `ENABLE_SIGNUP=false` in `.env` and `make restart`.

## Performance

Measured warm on the GPU host (one 14B ctx-baked model loaded). The first call after idle adds model-load time (~30 s cold). RAG chat and async extraction include the LLM; the other operations do not.

### Memory (kb-gateway)

| Operation | Endpoint | Median latency |
|---|---|---|
| add (queue -> 202, episode queued) | `POST /memory/add` | ~54 ms |
| retrieve facts (search) | `POST /memory/search` | ~89 ms |
| fetch raw episodes | `GET /memory/episodes?max=20` | ~57 ms |
| add -> fact searchable (async extraction, warm model) | add + poll `/memory/search` | ~9 s |

- `/memory/search` returns **facts** (`entity_edges`; fact text + `valid_at` / `invalid_at`) via Graphiti RAG: the query is embedded (`nomic-embed-text`), matched by vector similarity over nodes, then refined by graph traversal. The original `add` text is not returned.
- `/memory/episodes` returns the raw **episodes**: the original `add` texts, verbatim, in order. No embedding, no LLM, no graph traversal.

### RAG (Open Web UI)

| Operation | Endpoint | Median latency |
|---|---|---|
| raw chunk retrieval | `POST /api/v1/retrieval/query/collection` | ~64 ms (first call ~328 ms) |
| RAG chat (retrieval + grounded LLM) | `POST /api/chat/completions` (`files`) | ~1.5 s |

- Raw retrieval (`/api/v1/retrieval/query/collection`) returns **chunks**: a Chroma-style object (`distances`, `documents`, `metadatas`, `ids`), top-k by vector similarity. No LLM runs.
- RAG chat (`/api/chat/completions` with `files:[{"type":"collection","id":<kb-id>}]`) runs retrieval, injects the chunks as context, and returns a grounded LLM answer. The `make rag-config` strict template makes the model answer only from the retrieved chunks and refuse when the answer is absent.

## Security

The trust model in brief. For lockdown defaults, phone-home hardening, container caps, secrets handling, Neo4j auth, and dev-mode docs, see [docs/operations.md#hardening-reference](docs/operations.md#hardening-reference).

### Open WebUI (fronted by Caddy at `KB_HOST`)

- `WEBUI_AUTH=true`: every UI and REST call needs a session or a Bearer API key.
- First registered user becomes admin. Later signups get `DEFAULT_USER_ROLE=pending` and need admin approval. Close signup with `ENABLE_SIGNUP=false` + `make restart`.
- Knowledge bases are private by default; admins grant access to user groups (Workspace -> Knowledge). RBAC is additive: role + group membership.
- JWTs signed with a generated secret key (gitignored `.env.local`; `make start` rejects an empty/missing key). See [docs/operations.md#secrets-handling](docs/operations.md#secrets-handling).

### kb-gateway (`KB_HOST/memory/*`, `/admin/users`, `/health` → `:8010`)

- graphiti and Neo4j have no agent-facing auth and are **internal-only** on `graph_internal`. The kb-gateway is the sole bridge; [Caddy][caddy] (`KB_HOST`) proxies `/memory/*`, `POST /admin/users`, and `/health` to it. The Graphiti REST server has no native auth — the gateway is the gate.
- Every gateway endpoint (except `/health`) requires `Authorization: Bearer <KB_API_KEY>`. No key → `401`; bad key → `401`; Open WebUI unreachable → `503` (fail closed).
- `/health` is ungated on purpose: non-sensitive probe, carries no data or credentials. It also probes Open WebUI so `make health` catches an identity-broken stack rather than reporting healthy while auth is down.
- `KB_API_KEY` is a bearer — `KB_HOST` MUST be HTTPS or VPN/tunnel for any non-local agent. Plain HTTP is safe only on a trusted local interface.
- Rotate [Open WebUI][open-webui] API keys on a schedule (`FORCE=1 make api-keys`).

## Repository layout

| Path | Contents |
|---|---|
| `compose.yml` | services + networks (`graph_internal`, `edge`, `owui_net`); kb-gateway internal env |
| `gateway/` | kb-gateway: `app.py`, `authorize.py`, `graphiti.py`, `neo4j.py`, `owui.py`, `Dockerfile` (zero-dependency stdlib, non-root) |
| `caddy/Caddyfile` | public edge `KB_HOST`; routes `/memory/*`, `POST /admin/users`, `/health` → kb-gateway:8010, catch-all → openwebui:8080 |
| `graphiti/bootstrap.py` | mounted into the `graphiti` container and run as the command; injects `OpenAIGenericClient` + `nomic` embedder (768) so Ollama extraction works, runs a robust `/messages` worker, owns the app lifespan |
| `graphiti/config.yaml` | UNUSED — config for the retired MCP image; kept for reference (not mounted) |
| `Modelfile_qwen2_5` | reference Modelfile for the custom ctx-baked `MODEL_NAME` (`FROM qwen2.5:14b` + `PARAMETER num_ctx 8192`); see [docs/operations.md](docs/operations.md#custom-model-ctx-baked-variant) |
| `scripts/` | `bootstrap.sh`, `api-keys.sh`, `preflight.sh`, `rag-config.sh` |
| `skills/` | per-tool `kb` agent skill (`claude/` primary; `codex/`, `opencode/`, `pi/` symlink `scripts/` to it) — see [docs/agents.md](docs/agents.md) |
| `tests/` | `test_01`..`test_08` + `lib.sh` (see [docs/testing.md](docs/testing.md)) |
| `docs/` | `operations.md`, `testing.md`, `agents.md`, favicon assets |
| `.env` / `.env.example` | tracked template — no secrets (ports, tags, models, tunables, `OLLAMA_HOST`) |
| `.env.local` / `.env.local.example` | gitignored secrets + generated keys (`chmod 0600`) |
| `Makefile` | targets (see [docs/operations.md#make-targets](docs/operations.md#make-targets)) |
| `LICENSE` | MIT |

## Notes

- Embedding dimensions must be 768 for [`nomic-embed-text`][nomic-embed-text]. Change `EMBEDDER_MODEL` and `EMBEDDER_DIMENSIONS` together if you swap models. The bootstrap reads `EMBEDDER_DIMENSIONS` to set both the embedder and the vector index dim (Graphiti defaults to 1024, which would reject 768-dim writes).
- [Graphiti][graphiti] extraction uses **`json_schema` structured outputs** over Chat Completions. The image's default client targets the OpenAI Responses API (Ollama cannot satisfy it — extraction silently stores nothing), so `graphiti/bootstrap.py` injects the stock `OpenAIGenericClient` (>= 0.29 defaults to `json_schema`, which Ollama enforces server-side) at `temperature=0`. With plain `json_object` mode a 14B local model echoes the response schema back as values (Neo4j then rejects the nested MAP); `json_schema` prevents that.
- Extraction is **async**: `POST /memory/add` returns `200` as soon as the episode is queued, but each episode runs several LLM extraction calls before a fact is searchable. With the ctx-baked model (`num_ctx=8192`, ~20 GB, fits the GPU) a fact is searchable in ~20-40 s; do not treat a bare `200` (or even a stored episode) as proof of extraction — poll `/memory/search` for the probe.
- `OLLAMA_MODEL_BASE` **must be a non-reasoning model.** Graphiti 0.29.3 calls it with `max_tokens=16384`; a reasoning model (e.g. `gemma4:12b`) spends the budget on its thinking chain and emits no `content` (`finish_reason=length`) → `json.loads('')` → extraction silently stores nothing, even with `json_schema` enforcement. `qwen2.5:14b` is non-reasoning and tested reliable. Ollama's `/v1/chat/completions` does not honor `think=false`, so suppressing reasoning that way is not an option.
- The `/v1` endpoint ignores `num_ctx` in the request body, so `make pull-models` bakes `OLLAMA_MODEL_CONTEXT` (default `8192`) into `MODEL_NAME` via a `PARAMETER num_ctx` Modelfile (see [`Modelfile_qwen2_5`](Modelfile_qwen2_5) and [docs/operations.md](docs/operations.md#custom-model-ctx-baked-variant)). The remote GPU host (~22.5 GB VRAM) cannot hold the stock 14B at the default 32k context (~53 GB) — it spills to CPU and extraction crawls; `num_ctx=8192` loads ~20 GB and fits. `OPENWEBUI_MODEL` must equal `MODEL_NAME` so only one 14B instance loads. `make preflight` verifies the model exists and its `num_ctx` matches.
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