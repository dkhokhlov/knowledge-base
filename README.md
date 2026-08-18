# KnowledgeBase

Self-hosted knowledge base: Graphiti MCP (temporal knowledge graph over Neo4j) +
Open WebUI (document chat with RAG and user/group access control). Ollama runs on
the Docker host and supplies the chat LLM and `nomic-embed-text` embeddings.

## Architecture

```
+------------------------------------------------------------------------------+
|Host Ollama :11434  <--- host-gateway --->  kbnet (bridge)                    |
|                                                neo4j (internal)              |
|                                                graphiti-mcp (internal)       |
|0.0.0.0 exposed:                               graphiti-gateway               |
|  :3000 open-webui (HTTP + REST + /docs)       open-webui                     |
|  :8000 graphiti-gateway (/mcp/ bearer-gated)                                 |
|        \__ /health ungated                                                   |
+------------------------------------------------------------------------------+
```

- Only `:3000` (Open WebUI) and `:8000` (graphiti gateway) bind to 0.0.0.0.
- Neo4j and graphiti-mcp are container-network only (no host ports).
- The Caddy gateway checks `Authorization: Bearer <GRAPHITI_API_TOKEN>` on `/mcp/`.

## Prerequisites

- Docker >= 20.10 with the Compose plugin (`docker compose`).
- Ollama running on the Docker host. Pull the models you will use:
  ```
  make pull-models        # pulls MODEL_NAME (default qwen2.5:14b) + nomic-embed-text
  ```
  (equivalent to `ollama pull <MODEL_NAME>` and `ollama pull nomic-embed-text`)

## Quick start

1. Bootstrap the local secret file and data dirs:
   ```
   make bootstrap
   ```
2. Edit `.env.local` and set `GRAPHITI_API_TOKEN` to a value of your choice
   (this is the shared token your MCP clients will send). `WEBUI_SECRET_KEY`
   was generated for you.
3. Check the environment:
   ```
   make preflight
   ```
4. Start the stack:
   ```
   make start
   ```
5. Verify:
   ```
   make health
   ```
6. Open `http://<your-host-ip>:3000` and register the first user. The first
   user becomes the admin. Later signups need admin approval
   (`DEFAULT_USER_ROLE=pending`).
7. (Admin) In Settings -> Account -> API Keys, generate an API key for agent use.
8. (Optional) Close signup after bootstrap: set `ENABLE_SIGNUP=false` in `.env`
   and run `make restart`.

## Configuration

- `.env` — the config of record. Committed, no secrets. Edit ports, image tags,
  model names, Neo4j memory, and tunables here.
- `.env.local` — gitignored. Holds `WEBUI_SECRET_KEY` (generated) and
  `GRAPHITI_API_TOKEN` (you set it). Compose loads both via `env_file`.
- `graphiti/config.yaml` — Graphiti MCP config (LLM + embedder point at Ollama
  `/v1`; `nomic-embed-text`; 768 dimensions). Mounted read-only into the container.
- `caddy/Caddyfile` — the bearer-token gate. The token is read from Caddy's env,
  so the Caddyfile is safe to commit.

## Exposed endpoints

| Endpoint | Auth | Use |
|---|---|---|
| `http://<host>:3000` | session (web UI) | document upload, chat, admin, users, knowledge bases |
| `http://<host>:3000/api/*` | `Authorization: Bearer <OWUI API key>` | REST for agents |
| `http://<host>:3000/docs` | none (read-only) | Swagger UI |
| `http://<host>:3000/openapi.json` | none (read-only) | OpenAPI schema |
| `http://<host>:8000/mcp/` | `Authorization: Bearer <GRAPHITI_API_TOKEN>` | MCP (Streamable HTTP) |
| `http://<host>:8000/health` | none (read-only) | health probe |

Neo4j (`:7474`, `:7687`) and graphiti-mcp (`:8000` internal) are not published.

## Security

**Attack surface.** Only two ports bind to `0.0.0.0`: `:3000` (Open WebUI) and
`:8000` (Caddy gateway). Neo4j and graphiti-mcp have no host ports; they are
reachable only on the `kbnet` bridge network. Ollama is reached through the
host-gateway and is not published by this stack.

**Open WebUI (`:3000`).**
- `WEBUI_AUTH=true`: every UI and REST call needs a session or a Bearer API key.
- The first registered user becomes admin. Later signups get
  `DEFAULT_USER_ROLE=pending` and need admin approval.
- For a closed instance, set `ENABLE_SIGNUP=false` in `.env` and `make restart`.
- Agents use a per-user API key (`Authorization: Bearer sk-...`) generated in
  Settings -> Account -> API Keys. Admins can restrict API keys to specific
  routes (Settings -> Admin -> API Key Endpoint Restrictions).
- Knowledge bases are private by default. Admins grant access to user groups
  (Workspace -> Knowledge). RBAC is additive: role + group membership.
- JWTs are signed with `WEBUI_SECRET_KEY`, which is stable and stored only in
  gitignored `.env.local`. A `:?` guard in compose prevents boot without it.

**Graphiti MCP (`:8000`).**
- graphiti-mcp has no native auth, so the Caddy gateway requires
  `Authorization: Bearer <GRAPHITI_API_TOKEN>` on `/mcp/`. Requests without the
  token get `401`.
- `GRAPHITI_API_TOKEN` is a single shared secret (one token for all users), kept
  in gitignored `.env.local`. Caddy reads it from its env via
  `{$GRAPHITI_API_TOKEN}`, so the token never appears in the committed Caddyfile.
- Rotate the token by editing `.env.local` and running `make restart`.
- `/health` is ungated on purpose: it is a non-sensitive probe and carries no
  data or credentials.
- Shared-token means no per-user attribution for MCP calls. If you need
  per-user audit, front Caddy with a proxy that maps users to tokens.

**Secrets handling.**
- `.env` is committed and holds no secrets (ports, tags, model names, tunables).
- `.env.local` is gitignored and holds the only two secrets: `WEBUI_SECRET_KEY`
  (generated by `make bootstrap`) and `GRAPHITI_API_TOKEN` (you set it).
- Compose uses `${VAR:?...}` guards for both secrets, so the stack fails to
  start rather than boot unguarded.
- Never commit a fixed secret. If a secret leaks, rotate it and `make restart`.

**Neo4j.** Auth is on (`NEO4J_AUTH=neo4j/password`). It is not exposed to the
host. Set a stronger `NEO4J_PASSWORD` in `.env` for any non-local deployment.

**Open WebUI feature lockdown.** To reduce attack surface, Open WebUI ships
with the optional execution surfaces disabled by default (see `.env`):
- Tools and Skills: no workspace access and no importing, so users cannot
  upload/run arbitrary Python functions. `USER_PERMISSIONS_WORKSPACE_TOOLS_*`
  and `USER_PERMISSIONS_WORKSPACE_SKILLS_*` are `False`.
- Direct tool/MCP servers: `USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS=False`.
- Web search: `ENABLE_WEB_SEARCH=False` and
  `USER_PERMISSIONS_FEATURES_WEB_SEARCH=False`.
- OpenAI passthrough proxy: `ENABLE_OPENAI_API=False` — Open WebUI talks to
  Ollama only, no external OpenAI-compatible upstream.
- Community sharing and evaluation arenas: `ENABLE_COMMUNITY_SHARING=False`,
  `ENABLE_EVALUATION_ARENA_MODELS=False`.

These are *default* user permissions for a fresh install. An admin can still
grant Tools/Skills access to a user or group via the UI if a use case needs it.
The OpenAI-compatible agent REST API (`/api/chat/completions` etc.) is unaffected
by `ENABLE_OPENAI_API` — that flag only controls the external OpenAI *upstream*
model source, not Open WebUI's own API.

**Container hardening.** No service runs `privileged`. All four set
`security_opt: no-new-privileges:true`. The Caddy gateway and graphiti-mcp also
`cap_drop: ALL` (they have no host-owned bind-mount writes). Neo4j and Open WebUI
keep default capabilities because their entrypoints need `CAP_CHOWN` /
`DAC_OVERRIDE` to write to the host-owned `./data` bind mounts — dropping all
caps would break startup. To harden those two further, `chown` their `./data`
subdirs to the container user's UID and then add `cap_drop: ALL` (validate with
`make start` first).

**Dev-mode docs.** `ENV=dev` exposes `/docs` (Swagger UI) and `/openapi.json`
(schema) without auth. Both are read-only and do not mutate state or expose
credentials. If `:3000` is reachable from an untrusted network, put it behind a
reverse proxy with TLS, or limit the port with a firewall to trusted LAN/VPN.

**Hardening recommendations.**
- Front `:3000` and `:8000` with a TLS reverse proxy for remote access.
- Use firewall rules to limit both ports to trusted networks or a VPN.
- Rotate `GRAPHITI_API_TOKEN` and Open WebUI API keys on a schedule.
- Protect `./data`: it holds `webui.db` (user credential hashes), uploaded
  documents, and the Neo4j graph. Restrict file permissions; when you move it
  to RAID, ensure the RAID volume keeps restrictive permissions.
- Keep image tags pinned (as in `.env`) and pull patches with `make pull`.

## Agent usage

### Graphiti MCP (HTTP)

Point your MCP client at `http://<host>:8000/mcp/` with the header
`Authorization: Bearer <GRAPHITI_API_TOKEN>`. Manual check:

```
curl -X POST http://localhost:8000/mcp/ \
  -H 'Authorization: Bearer <GRAPHITI_API_TOKEN>' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-03-26","capabilities":{},
                 "clientInfo":{"name":"verify","version":"1"}}}'
```

### Open WebUI REST

Base path `/api`. Chat (OpenAI-compatible), file upload, and knowledge binding:

```
# chat
curl -s http://localhost:3000/api/chat/completions \
  -H "Authorization: Bearer <OWUI API key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:14b","messages":[{"role":"user","content":"Say hello."}]}'

# upload a file (returns an id)
curl -s http://localhost:3000/api/v1/files/ \
  -H "Authorization: Bearer <OWUI API key>" \
  -F 'file=@./note.txt'

# bind the file to a knowledge collection
curl -s -X POST http://localhost:3000/api/v1/knowledge/<kb-id>/file/add \
  -H "Authorization: Bearer <OWUI API key>" \
  -H "Content-Type: application/json" \
  -d '{"file_id":"<file-id>"}'
```

Generate the OWUI API key in the UI: Settings -> Account -> API Keys.
Admins grant knowledge-base access to user groups via Workspace -> Knowledge.

## Persistent data and moving to RAID

All state is under `./data` (bind mounts, not named volumes). To move it to RAID:

```
make stop
mv ./data /mnt/RAID/kb/data
ln -s /mnt/RAID/kb/data ./data
make start
```

`DATA_ROOT=./data` resolves through the symlink, so no `.env` edit is needed.

## Make targets

| target | action |
|---|---|
| `help` | show targets (default) |
| `bootstrap` | create `.env.local` (generate `WEBUI_SECRET_KEY`) and `./data` dirs |
| `preflight` | read-only checks: docker, secrets set, Ollama, models |
| `pull` | pull images |
| `pull-models` | pull Ollama models (`MODEL_NAME` + `nomic-embed-text`) on the host |
| `start` | `docker compose up -d` |
| `stop` | `docker compose stop` (keeps containers and data) |
| `restart` | stop then start |
| `logs` | tail logs |
| `ps` | container status (with health) |
| `config` | render effective compose config |
| `health` | probe graphiti and Open WebUI `/health` |
| `shell-owui` / `shell-neo4j` / `shell-graphiti` / `shell-caddy` | exec a shell |
| `clear` | `down --remove-orphans`; KEEPS `./data` and `.env.local` |
| `clear-all` | `down --volumes` + delete `./data` + delete `.env.local` |

`clear` preserves all state (clean recreate). `clear-all` wipes data and the
generated secret; `.env`, `graphiti/config.yaml`, and `caddy/Caddyfile` are kept.

## Notes and risks

- Embedding dimensions must be 768 for `nomic-embed-text`. Change
  `EMBEDDER_MODEL` and `EMBEDDER_DIMENSIONS` together if you swap models.
- Graphiti uses `json_object` structured output (Ollama does not support
  `json_schema`).
- `GRAPHITI_API_TOKEN` is a shared secret. Rotate it in `.env.local` then
  `make restart`.
- Open WebUI exposes `/docs` and `/openapi.json` only in `ENV=dev`, which also
  raises log verbosity. Acceptable for an internal/LAN deployment.
- `SEMAPHORE_LIMIT=3` is conservative for one local Ollama; raise it if the
  host Ollama has capacity.

## License

MIT — see `LICENSE`. Copyright (c) 2026 Dmitri Khokhlov <dkhokhlov@gmail.com>.