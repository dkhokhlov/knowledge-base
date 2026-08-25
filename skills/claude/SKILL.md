---
name: kb
description: Use when the user wants to query or chat with a self-hosted Open WebUI knowledge base (KB) over REST, to index/search/retrieve Claude projects memory (~/.claude/projects/*/memory), or to remember/search/retrieve Graphiti facts memory. Triggers on "KB"/"knowledge base", "index projects memory", "search projects memory"/"retrieve projects memory", "projects/repo index status", "remember …", "what do we know about …", and "forget …". Covers list/search KBs, retrieve (semantic search) from a KB, RAG chat grounded on a KB, projects memory (index-projects/retrieve-projects/status-projects via owui.py — user key creates + owns one KB per project), and Graphiti facts memory (add/retrieve/episodes/forget via the kb-gateway). One URL (KB_HOST) fronts OWUI REST (root /api/*) and kb-gateway memory (/memory/*). Authenticates with KB_API_KEY (an Open WebUI key; read-scoped for KBs the caller does not own, write-scoped for the caller's own project KBs; identity+role derived server-side by the kb-gateway for facts memory). Includes zero-dependency Python CLI wrappers (scripts/owui.py for OWUI KBs + projects memory, scripts/kb_gateway.py for Graphiti facts memory).
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

Two ways to query a KB — **default to `retrieve`**: read the raw chunks and
synthesize the answer yourself. Use `rag` only for a quick one-shot answer when
the local model is adequate and the token cost of returning chunks matters.

### Retrieve  (default)

`POST /api/v1/retrieval/query/collection` — body `{collection_names:[<kb-id>], query, k, hybrid:true}` → **Chroma** `{documents:[[…]], distances:[[…]], metadatas:[[…]], ids:[[…]]}` (one inner list per collection_name).

- Pure vector retrieval — **no LLM call**. Returns matched chunks + distances.
- **Use when**: the default — read the chunks and synthesize yourself so the answer is correct and you keep control of the reasoning.
- **Cost**: more of your tokens (chunks return); zero Ollama.
- **Risk**: none from synthesis (you do it). Lower distance = better match (Chroma cosine, 0 best).
- **Response is nested arrays** (Chroma shape). Flatten `documents`/`distances`/`metadatas`/`ids` per collection before reading. The wrapper does this; if calling curl directly, parse with care.
- Wrapper: `retrieve <kb-id> "<query>" [--k N] [--no-hybrid]`.

### Chat (RAG)

The `/kb` skill reaches RAG via the kb-gateway: `POST /memory/rag` — body
`{messages, files:[{type:collection,id:<kb-id>}]}` (NO `model` field) →
`{content}`. The gateway inserts the chat model server-side (from
`OPENWEBUI_MODEL`) and forwards the caller's `KB_API_KEY` to OWUI, so OWUI
enforces KB read access natively. (Humans/admins still RAG directly at
`POST /api/chat/completions` with an explicit `model` field — see [Admin surface](#admin-surface).)

- The server vector-searches the `files` collection, injects the chunks into the strict `RAG_TEMPLATE`, and calls the chat LLM for a grounded answer.
- **Requires `make rag-config`**: the strict `RAG_TEMPLATE` is set by `make rag-config`, not the image default — without it the model falls back to its own knowledge and confabulates. The embedding URL must also be in sync (`make preflight` checks; `make rag-config` re-syncs). See Prerequisites.
- **Use when**: a quick one-shot answer is enough, the local model is adequate, and you accept its synthesis. Not the default — prefer `retrieve` when correctness matters or you want to reason over the chunks yourself.
- **Cost**: fewer of your tokens (only the answer returns); spends Ollama tokens.
- **Risk**: the local model can confabulate. If the answer must be right, use **Retrieve** above and synthesize yourself.
- **Grounding**: pass the KB via the top-level `files` field only. Do NOT use a `knowledge` field (silently ignored) or `metadata.knowledge` (request metadata is discarded and replaced server-side). `type:collection` = whole-KB vector search; `type:file` = one file id. The caller needs read access to the KB; the model is backend-side config (the gateway inserts it).
- Wrapper: `rag "<question>" --kb <kb-id> [--kb <id2>]`.

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
read-only and matches the agent role; the **projects-memory surface**
(index-projects/retrieve-projects/status-projects) writes to KBs the caller owns
(see [Projects memory](#projects-memory-claude-project-memory--owui-kbs-host-side-user-key) below). The wrapper lives in `scripts/` next to this file; set `S` to its
path in your installed copy of this skill.

```
S=~/.claude/skills/kb/scripts/owui.py
export KB_HOST=http://localhost:3000            # or your KB_HOST
export KB_API_KEY="$OPENWEBUI_USER_API_KEY"     # from .env.local (make api-keys)

python3 "$S" whoami                             # verify key + role
python3 "$S" kbs                                # list visible KBs
python3 "$S" search-kbs "main"                  # find a KB by name
python3 "$S" retrieve <kb-id> "XSL streaming"     # retrieve — raw chunks, you synthesize (default)
python3 "$S" rag "What is XSL?" --kb <kb-id>    # chat (RAG) — one-shot LLM answer from the KB (via kb-gateway)
python3 "$S" file <file-id>                     # file text content
```

Config: the wrapper is a thin client. It reads ONLY `KB_HOST` and `KB_API_KEY`
from the shell environment — no `.env` / `.env.local` files, no other env vars,
no `--base-url` / `--key` / `--model` flags. Set both in your shell before
invoking it (`export KB_HOST=...` / `export KB_API_KEY=...`). RAG chat is
proxied by the kb-gateway (`POST /memory/rag`), which inserts the chat model
server-side from `OPENWEBUI_MODEL`; the wrapper carries no model.

Typical flow: `kbs` (or `search-kbs`) → grab the KB id → `retrieve` for raw
chunks (the default — you synthesize), or `rag` for a one-shot answer (see the
Retrieve vs Chat (RAG) groups above).

# Projects memory (Claude project memory → OWUI KBs, host-side, user key)

**Projects memory** = Claude's per-project auto-memory
(`~/.claude/projects/<encoded-dir>/memory/*.md`), indexed into OWUI KBs — one KB
per project — so an agent can recall knowledge accumulated across Claude Code
projects and sessions. (Distinct from [facts memory](#facts-memory-graphiti-agent--kb-gateway)
below, which is the Graphiti knowledge graph.) The skill-side wrapper walks the
host filesystem and calls OWUI REST **directly with the caller's user key**: the
caller creates + owns each project KB (`KB.user.email == caller`), so retrieve
filters KBs by owner. The kb-gateway is not involved (it has no user-key OWUI-KB
write path; its `/index` uses the admin key).

## One-time setup

OWUI gates KB creation on the `workspace.knowledge` permission, which is off by
default in this deployment. Enable it once (admin), then never again:

```
make projects-bootstrap   # admin: enable workspace.knowledge + verify with a user-key probe KB
```

`index-projects` fails with a clear message if this has not been run.

## KB naming + metadata

- KB name = `<host>--<encoded-dir-without-leading-dash>`. Host = short hostname
  (`platform.node()`, or `--host`). Example: project
  `/home/user/projects/myrepo` → encoded
  `-home-user-projects-myrepo` → KB
  `<host>--home-user-projects-myrepo`.
- Per-file metadata (flat in `File.meta.data`): `host`, `project` (exact encoded
  dir), `project_path` (decode; authoritative when the path exists on disk, else
  lossy), `repo` (git repo name = path basename), `account` (caller email),
  `source_relpath` (`memory/<file>`). `repo` is the human-friendly identifier for
  reasoning about hits; it also rides in the KB `description` so `kbs`/`kb` show it.

## Workflow

- **At session start**, run `index-projects` so this session's accumulated
  projects memory is in the KB and searchable across sessions/repos (same
  "refresh at session start" convention as open-codebase-index).
- **On an explicit user prompt** ("index projects memory"), re-run
  `index-projects` to refresh the KB for another repo's session.
- After indexing, run `status-projects` to confirm the current repo's drain is
  done before relying on retrieval.

`index-projects` is a full snapshot every run: it always re-uploads
`memory/*.md` (OWUI idempotency reuses unchanged files, no re-extract); a
**modified** file is delete-then-uploaded (router `DELETE` cleans the old
vectors — the upload's own reclaim does not); and orphans (source file gone)
are deleted so the KB mirror stays exact.

## Using the wrapper (`scripts/owui.py`, projects-memory subcommands)

```
S=~/.claude/skills/kb/scripts/owui.py
# (KB_HOST + KB_API_KEY already exported above)

python3 "$S" index-projects --dry-run              # plan only; JSON {projects,total}
python3 "$S" index-projects --project knowledgebase --wait   # index this repo, then wait for drain
python3 "$S" status-projects                       # current repo's drain status (JSON; walks up cwd)
python3 "$S" retrieve-projects "QPU scheduling"      # across ALL your project KBs
python3 "$S" retrieve-projects "memory" --host <host> --project knowledgebase   # filtered
python3 "$S" retrieve-projects "XSL" --kb-glob '<host>--*'   # wildcard KB name
```

`retrieve-projects` filters: `--host` (name starts with `<host>--`), `--project`
(substring in the project part), `--account` (KB owner email; default = caller,
aka `--mine`), `--kb-glob` (fnmatch on the KB name). No filters = all KBs you
own. It makes one retrieval call per KB (hit metadata carries no `knowledge_id`,
so one-call-per-KB is the reliable attribution) and prints compact JSON
`{"kbs":N,"hits":[{"repo","kb_name","file","text",...}],"errors":[...]}`.

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
G=~/.claude/skills/kb/scripts/kb_gateway.py
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

In Claude Code, the most reliable trigger is the slash command. Natural
phrasing also triggers it when it matches the skill description. Both "KB" and
"knowledge base" phrasings are recognized.

| Trigger | Example phrasing | Deterministic? |
|---|---|---|
| Slash command | `/kb` then the request | yes |
| Natural — search | "search the KB for X" / "search the knowledge base for X" | by description match |
| Natural — RAG chat | "ask the KB: \<question\>" / "ask the knowledge base: \<question\>" | by description match |
| Natural — list | "list my KBs" / "list my knowledge bases" | by description match |
| Natural — index projects memory | "index projects memory" / "index my Claude project memory" / "index project memory into KBs" | by description match |
| Natural — projects status | "projects memory index status" / "is my repo indexed" / "index status for this repo" | by description match |
| Natural — search projects memory | "search projects memory for X" / "search across my project KBs for X" | by description match |
| Natural — remember | "remember that …" / "what do we know about …" / "forget …" | by description match |

## Install location

This skill is installed at `~/.claude/skills/kb/` (the `scripts/` directory
above is `~/.claude/skills/kb/scripts/`). Claude Code auto-discovers skills
there by their `SKILL.md` frontmatter.