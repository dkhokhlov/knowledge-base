---
name: kb
description: Use when the user wants to query or chat with a self-hosted Open WebUI knowledge base (KB) over REST, or to remember/search/retrieve Graphiti facts memory. Triggers on "KB"/"knowledge base", "remember …", "what do we know about …", and "forget …". Covers list/search KBs, retrieve (semantic search) from a KB, RAG chat grounded on a KB, and Graphiti facts memory (add/retrieve/episodes/forget via the kb-gateway). One URL (KB_HOST) fronts OWUI REST (root /api/*) and kb-gateway memory (/memory/*). Authenticates with KB_API_KEY (an Open WebUI key; read-scoped for KBs the caller does not own, write-scoped for the caller's own project KBs; identity+role derived server-side by the kb-gateway for facts memory). Includes zero-dependency Python CLI wrappers (scripts/owui.py for OWUI KBs, scripts/kb_gateway.py for Graphiti facts memory).
---

# Open WebUI REST (agent / read-scoped)

Drive a self-hosted Open WebUI knowledge base over REST with the **non-admin
agent API key** — read-only scope. This skill covers only what an agent can do:
list/search KBs, retrieve (semantic) from a KB, RAG chat grounded on a KB, and read file
content. Write/delete/admin operations are out of scope (see [Admin surface](#admin-surface)).

The whole stack is fronted by Caddy at one URL, **KB_HOST** (default
`http://localhost:3000`). OWUI REST is at the KB_HOST root (`/api/*`); the
kb-gateway memory endpoints are at `/memory/*` on the same host. One URL, one
key.

## Prerequisites

- The stack is running and healthy (`make start && make health` in the
  knowledgebase repo).
- The agent key exists: produced by `make api-keys`, which writes
  `OPENWEBUI_USER_API_KEY` into the gitignored `.env.local` and grants the agent
  `*` read on the chat model so RAG chat works.
- Grounded RAG is configured: `make rag-config` has been run. It sets the strict
  `RAG_TEMPLATE` (answer only from the KB context — without it, ~12B models
  confabulate from their own knowledge) and syncs `rag.ollama.base_url` to `.env`
  `OLLAMA_BASE_URL` (OWUI persists that URL on first boot and ignores later `.env`
  changes; a stale value breaks embedding). `make preflight` warns if it drifts;
  re-run `make rag-config` to fix, and after any DB reset/rebuild.
- Set `KB_HOST` and `KB_API_KEY` in your shell env (`export KB_HOST=...`,
  `export KB_API_KEY=...`). The wrapper is a thin client: it reads ONLY those two
  env vars and does not read `.env` / `.env.local`. The agent key is
  `OPENWEBUI_USER_API_KEY` (written to the gitignored `.env.local` by `make api-keys`).

## Auth

- Header: `Authorization: Bearer $KB_API_KEY` (the agent key is also stored as
  `OPENWEBUI_USER_API_KEY` in `.env.local`).
- The key belongs to a `user`-role account (`agent@<KB_DOMAIN>`, default `agent@local.test`), not admin.
- Read scope: sees KBs via their `*` (public) read grants; `write_access=false`
  on every KB it does not own. It cannot `file/add`, remove files, or delete a KB.

## Agent endpoints

Two ways to query a KB — pick by what you need:

### Chat (RAG)

The `/kb` skill reaches RAG via the kb-gateway: `POST /memory/rag` — body
`{messages, files:[{type:collection,id:<kb-id>}]}` (NO `model` field) →
`{content}`. The gateway inserts the chat model server-side (from
`OPENWEBUI_MODEL`) and forwards the caller's `KB_API_KEY` to OWUI, so OWUI
enforces KB read access natively. (Humans/admins still RAG directly at
`POST /api/chat/completions` with an explicit `model` field — see [Admin surface](#admin-surface).)

- The server vector-searches the `files` collection, injects the chunks into the strict `RAG_TEMPLATE`, and calls the chat LLM for a grounded answer.
- **Requires `make rag-config`**: the strict `RAG_TEMPLATE` is set by `make rag-config`, not the image default — without it the model falls back to its own knowledge and confabulates. The embedding URL must also be in sync (`make preflight` checks; `make rag-config` re-syncs). See Prerequisites.
- **Use when**: a one-shot answer is enough and the local model is adequate.
- **Cost**: fewer of your tokens (only the answer returns); spends Ollama tokens.
- **Risk**: the local model can confabulate. If the answer must be right, use **Retrieve** below and synthesize yourself.
- **Grounding**: pass the KB via the top-level `files` field only. Do NOT use a `knowledge` field (silently ignored) or `metadata.knowledge` (request metadata is discarded and replaced server-side). `type:collection` = whole-KB vector search; `type:file` = one file id. The caller needs read access to the KB; the model is backend-side config (the gateway inserts it).
- Wrapper: `rag "<question>" --kb <kb-id> [--kb <id2>]`.

### Retrieve

`POST /api/v1/retrieval/query/collection` — body `{collection_names:[<kb-id>], query, k, hybrid:true}` → **Chroma** `{documents:[[…]], distances:[[…]], metadatas:[[…]], ids:[[…]]}` (one inner list per collection_name).

- Pure vector retrieval — **no LLM call**. Returns matched chunks + distances.
- **Use when**: the answer must be correct — read the chunks and synthesize yourself.
- **Cost**: more of your tokens (chunks return); zero Ollama.
- **Risk**: none from synthesis (you do it). Lower distance = better match (Chroma cosine, 0 best).
- **Response is nested arrays** (Chroma shape). Flatten `documents`/`distances`/`metadatas`/`ids` per collection before reading. The wrapper does this; if calling curl directly, parse with care.
- Wrapper: `retrieve <kb-id> "<query>" [--k N] [--no-hybrid]`.

### Discovery and file content

| Method | Path | Body / qs | Returns |
|---|---|---|---|
| GET | `/api/v1/auths/` | — | whoami: `{email, role}` |
| GET | `/api/v1/knowledge/` | — | `{items:[{id,name,description,file_count,write_access}]}` |
| GET | `/api/v1/knowledge/{id}` | — | one KB metadata |
| GET | `/api/v1/knowledge/search` | `?query=<text>` | KB name search |
| GET | `/api/v1/files/{id}/content` | — | file text content |

## Phone-home (RAG is safe)

RAG chat / semantic retrieval drives the Chroma vector client. Chroma telemetry is
**off**: Open WebUI builds the chromadb client with `anonymized_telemetry=False`
and the container env sets `ANONYMIZED_TELEMETRY=false`. No outbound telemetry
on retrieve/chat. (Other phone-home hardening — Graphiti/posthog, OWUI version
check, favicon, OTEL — is handled in the stack's compose/`.env`.)

## Admin surface

This skill is agent-scoped only. The full API (file upload, KB create/delete,
file bind/remove, access grants, user/admin config, retrieval processing) is
documented elsewhere:

- **OpenAPI schema**: `GET /openapi.json` (JSON; no auth to fetch).
- **Swagger UI**: `/api/docs` (interactive; admin key to exercise write endpoints).

Use `OPENWEBUI_ADMIN_API_KEY` (full admin, bypasses access control) for those —
keep it private; do not hand it to agents.

## Using the wrapper (`scripts/owui.py`)

Zero-dependency (Python 3.10+ stdlib). The KB surface (kbs/retrieve/rag/file) is
read-only and matches the agent role. The wrapper lives in `scripts/` next to this file; set `S` to its
path in your installed copy of this skill.

```
S=~/.config/opencode/skills/kb/scripts/owui.py
export KB_HOST=http://localhost:3000            # or your KB_HOST
export KB_API_KEY="$OPENWEBUI_USER_API_KEY"     # from .env.local (make api-keys)

python3 "$S" whoami                             # verify key + role
python3 "$S" kbs                                # list visible KBs
python3 "$S" search-kbs "main"                  # find a KB by name
python3 "$S" rag "What is XSL?" --kb <kb-id>    # chat (RAG) — LLM answer from the KB (via kb-gateway)
python3 "$S" retrieve <kb-id> "XSL streaming"     # retrieve — raw chunks, you synthesize
python3 "$S" file <file-id>                     # file text content
```

Config: the wrapper is a thin client. It reads ONLY `KB_HOST` and `KB_API_KEY`
from the shell environment — no `.env` / `.env.local` files, no other env vars,
no `--base-url` / `--key` / `--model` flags. Set both in your shell before
invoking it (`export KB_HOST=...` / `export KB_API_KEY=...`). RAG chat is
proxied by the kb-gateway (`POST /memory/rag`), which inserts the chat model
server-side from `OPENWEBUI_MODEL`; the wrapper carries no model.

Typical flow: `kbs` (or `search-kbs`) → grab the KB id → `rag` for a one-shot
answer, or `retrieve` for raw chunks when the answer must be right (see the
Retrieve vs Chat (RAG) groups above).

# Facts memory (Graphiti, agent / kb-gateway)

Fact memory lives in Graphiti (Neo4j). Agents reach it **only** through the
`kb-gateway`, fronted by Caddy at **KB_HOST** under `/memory/*`. The gateway
derives your identity + role from your `KB_API_KEY` via Open WebUI (tamper-proof
— you cannot set your own identity). Authorization is server-side.

## Prerequisites

- The stack is up and healthy (`make start && make health`).
- You have a `KB_API_KEY` (an Open WebUI key). The admin's is
  `OPENWEBUI_ADMIN_API_KEY`; per-account keys are issued by the admin via
  `make users-create` (an operator make target, not the skill). Set `KB_HOST`
  (default `http://localhost:3000`) and `KB_API_KEY` in your shell env — the
  wrapper reads only those two (no `--env-file`).
- For non-local `KB_HOST`, the URL must be HTTPS or a VPN/tunnel (`KB_API_KEY`
  is a bearer). On a trusted local interface plain HTTP is fine.

## Model

- **Writes go to your own personal group.** `add` with no `--group` writes to
  your personal group `user:<email>` (stored by Graphiti as `user-<sanitized-email>`,
  e.g. `user-agent-local-test`; `forget` accepts `user:<email>` too). `add --group G` is allowed only if `G` is
  your own personal group; any other group → `403`. There are no shared write
  groups — reads are how knowledge is shared across accounts.
- **Reads see ALL groups that have data, read-only.** `retrieve` and `episodes`
  span every group the gateway discovers live from Neo4j (no roster file —
  future groups and out-of-band writes are included automatically).
- **Destructive ops require owning the target group or admin.** `forget`
  clears one group's memory (your own group, or admin). `delete-edge`/
  `delete-episode` delete one item by uuid: the gateway looks up the item's
  group and requires you own that group (or are admin); unknown uuids are
  rejected (fail-closed).
- **`status` is global** (Graphiti server + DB health); no group scoping.

## Using the wrapper (`scripts/kb_gateway.py`)

Zero-dependency (Python 3.10+ stdlib). One client for memory + admin ops. The
wrapper lives in `scripts/` next to this file; set `G` to its path in your
installed copy of this skill.

```
G=~/.config/opencode/skills/kb/scripts/kb_gateway.py
# (KB_HOST + KB_API_KEY already exported — see Prerequisites above)

python3 "$G" whoami                              # verify identity (from the key, via the gateway)
python3 "$G" groups                              # list all groups that have data
python3 "$G" add "Project Atlas uses a QPU scheduler" --name atlas
python3 "$G" retrieve "QPU scheduling" --k 5       # facts across ALL groups (read-only)
python3 "$G" episodes --max 20                   # episodes across ALL groups (read-only)
python3 "$G" status                              # graphiti server + DB status
python3 "$G" forget user:alice@example.com       # clear YOUR group's memory (owner/admin)
python3 "$G" delete-edge <uuid>                  # delete one edge (owner/admin of its group)
python3 "$G" delete-episode <uuid>               # delete one episode (owner/admin of its group)
```

**`add` is asynchronous:** Graphiti extracts entity edges in a background
Ollama pass after `add` returns, so an immediate `retrieve` for the just-added
fact can return `[]`. Wait, or retry, before treating a 0-hit retrieve as "not
remembered" — observed latency on this deployment (kb host → GPU Ollama host,
`qwen2.5:14b`): ~10-15s warm, and a cold start (model not loaded) can exceed
90s. Varies by host/model.

Errors: any non-200 from the gateway exits non-zero with the gateway's message
(401 = bad/missing key; 403 = not authorized for that op/group; 503 = identity
service down; 502 = graphiti/neo4j down; 501 = admin op unsupported by this
Open WebUI image).

## Triggering this skill

OpenCode auto-discovers skills by their `SKILL.md` frontmatter (`name` +
`description`) and loads the matching skill on demand — no slash command needed.
Both "KB" and "knowledge base" phrasings are recognized by the description.

| Trigger | Example phrasing | Deterministic? |
|---|---|---|
| Auto-discovery | "search the KB for X" (matches the skill description) | by description match |
| Natural — search | "search the KB for X" / "search the knowledge base for X" | by description match |
| Natural — RAG chat | "ask the KB: \<question\>" / "ask the knowledge base: \<question\>" | by description match |
| Natural — list | "list my KBs" / "list my knowledge bases" | by description match |
| Natural — remember | "remember that …" / "what do we know about …" / "forget …" | by description match |

## Install location

This skill is installed at `~/.config/opencode/skills/kb/` (the `scripts/`
directory above is `~/.config/opencode/skills/kb/scripts/`). OpenCode also
discovers skills at `.opencode/skills/kb/` (project-local) and, for
Claude-compatible installs, `~/.claude/skills/kb/`; extra paths can be added via
`skills.paths` in `opencode.json`.