---
name: kb
description: Use when the user wants to query or chat with a self-hosted Open WebUI knowledge base (KB) over REST, to index/search Claude projects memory (~/.claude/projects/*/memory), to remember/search Graphiti facts memory, or (as an admin) to create a new KB user. Triggers on "KB"/"knowledge base", "index projects memory", "search projects memory", "projects/repo index status", "remember …", "what do we know about …", "forget …", and "create a new KB user …". Covers list/search KBs, semantic-search a KB, RAG chat grounded on a KB, projects memory (index-projects/search-projects/status-projects via owui.py — user key creates + owns one KB per project), Graphiti facts memory (add/search/episodes/forget via the kb-gateway), and admin user provisioning. One URL (KB_HOST) fronts OWUI REST (root /api/*) and kb-gateway memory (/memory/*). Authenticates with KB_API_KEY (an Open WebUI key; read-scoped for KBs the caller does not own, write-scoped for the caller's own project KBs; identity+role derived server-side by the kb-gateway for facts memory). Includes zero-dependency Python CLI wrappers (scripts/owui.py for OWUI KBs + projects memory, scripts/kb_gateway.py for Graphiti facts memory + admin).
---

# Open WebUI REST (agent / read-scoped)

Drive a self-hosted Open WebUI knowledge base over REST with the **non-admin
agent API key** — read-only scope. This skill covers only what an agent can do:
list/search KBs, semantic-search a KB, RAG chat grounded on a KB, and read file
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
- `KB_HOST` lives in `.env`; the key (`KB_API_KEY` / `OPENWEBUI_USER_API_KEY`)
  lives in `.env.local`. Load both with `--env-file` (repeatable), or export the
  env vars.

## Auth

- Header: `Authorization: Bearer $KB_API_KEY` (the agent key is also stored as
  `OPENWEBUI_USER_API_KEY` in `.env.local`).
- The key belongs to a `user`-role account (`agent@<KB_DOMAIN>`, default `agent@local.test`), not admin.
- Read scope: sees KBs via their `*` (public) read grants; `write_access=false`
  on every KB it does not own. It cannot `file/add`, remove files, or delete a KB.

## Agent endpoints

Two ways to query a KB — pick by what you need:

### Chat (RAG)

`POST /api/chat/completions` — body `{model, files:[{type:collection,id:<kb-id>}], messages, stream:false}` → `{choices:[{message:{content}}]}`.

- The server vector-searches the `files` collection, injects the chunks into the strict `RAG_TEMPLATE`, and calls the chat LLM (default `gemma4:12b`) for a grounded answer.
- **Requires `make rag-config`**: the strict `RAG_TEMPLATE` is set by `make rag-config`, not the image default — without it the model falls back to its own knowledge and confabulates. The embedding URL must also be in sync (`make preflight` checks; `make rag-config` re-syncs). See Prerequisites.
- **Use when**: a one-shot answer is enough and the local ~12B model is adequate.
- **Cost**: fewer of your tokens (only the answer returns); spends Ollama tokens.
- **Risk**: the local model can confabulate. If the answer must be right, use **Search** below and synthesize yourself.
- **Grounding**: pass the KB via the top-level `files` field only. Do NOT use a `knowledge` field (silently ignored) or `metadata.knowledge` (request metadata is discarded and replaced server-side). `type:collection` = whole-KB vector search; `type:file` = one file id. `model` defaults to the stack's chat LLM. The caller needs read access to the KB and to the model.
- Wrapper: `rag "<question>" --kb <kb-id> [--kb <id2>] [--model <m>]`.

### Search

`POST /api/v1/retrieval/query/collection` — body `{collection_names:[<kb-id>], query, k, hybrid:true}` → **Chroma** `{documents:[[…]], distances:[[…]], metadatas:[[…]], ids:[[…]]}` (one inner list per collection_name).

- Pure vector retrieval — **no LLM call**. Returns matched chunks + distances.
- **Use when**: the answer must be correct — read the chunks and synthesize yourself.
- **Cost**: more of your tokens (chunks return); zero Ollama.
- **Risk**: none from synthesis (you do it). Lower distance = better match (Chroma cosine, 0 best).
- **Response is nested arrays** (Chroma shape). Flatten `documents`/`distances`/`metadatas`/`ids` per collection before reading. The wrapper does this; if calling curl directly, parse with care.
- Wrapper: `search <kb-id> "<query>" [--k N] [--no-hybrid]`.

### Discovery and file content

| Method | Path | Body / qs | Returns |
|---|---|---|---|
| GET | `/api/v1/auths/` | — | whoami: `{email, role}` |
| GET | `/api/v1/knowledge/` | — | `{items:[{id,name,description,file_count,write_access}]}` |
| GET | `/api/v1/knowledge/{id}` | — | one KB metadata |
| GET | `/api/v1/knowledge/search` | `?query=<text>` | KB name search |
| GET | `/api/v1/files/{id}/content` | — | file text content |

## Phone-home (RAG is safe)

RAG chat / semantic search drives the Chroma vector client. Chroma telemetry is
**off**: Open WebUI builds the chromadb client with `anonymized_telemetry=False`
and the container env sets `ANONYMIZED_TELEMETRY=false`. No outbound telemetry
on search/chat. (Other phone-home hardening — Graphiti/posthog, OWUI version
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

Zero-dependency (Python 3.8+ stdlib). The KB surface (kbs/search/rag/file) is
read-only and matches the agent role; the **projects-memory surface**
(index-projects/search-projects/status-projects) writes to KBs the caller owns
(see [Projects memory](#projects-memory-claude-project-memory--owui-kbs-host-side-user-key) below). The wrapper lives in `scripts/` next to this file; set `S` to its
path in your installed copy of this skill.

```
KB=~/SOURCE/Deployments/knowledgebase
S=~/.pi/agent/skills/kb/scripts/owui.py
E="--env-file $KB/.env --env-file $KB/.env.local"   # KB_HOST in .env, key in .env.local

python3 "$S" $E whoami                          # verify key + role
python3 "$S" $E kbs                             # list visible KBs
python3 "$S" $E search-kbs "main"               # find a KB by name
python3 "$S" $E rag "What is XSL?" --kb <kb-id>  # chat (RAG) — LLM answer from the KB
python3 "$S" $E search <kb-id> "XSL streaming"  # search — raw chunks, you synthesize
python3 "$S" $E file <file-id>                   # file text content
```

Config resolution: `--base-url`/`--key` flags > `--env-file` (repeatable; load
`.env` then `.env.local`; later files override earlier, and explicit files
override inherited shell env — same precedence as `make api-keys`, which sources
`.env`/`.env.local`) > inherited `KB_HOST` (or `KB_HOST_PORT`) + `KB_API_KEY`
(fallback `OPENWEBUI_USER_API_KEY`). RAG model: `--model` or `OPENWEBUI_MODEL` or
`MODEL_NAME` env, default `gemma4:12b` — resolved from `.env` so the wrapper
requests the same model `make api-keys` grants `*` read on.

Typical flow: `kbs` (or `search-kbs`) → grab the KB id → `rag` for a one-shot
answer, or `search` for raw chunks when the answer must be right (see the
Chat (RAG) vs Search groups above).

# Projects memory (Claude project memory → OWUI KBs, host-side, user key)

**Projects memory** = Claude's per-project auto-memory
(`~/.claude/projects/<encoded-dir>/memory/*.md`), indexed into OWUI KBs — one KB
per project — so an agent can recall knowledge accumulated across Claude Code
projects and sessions. (Distinct from [facts memory](#facts-memory-graphiti-agent--kb-gateway)
below, which is the Graphiti knowledge graph.) The skill-side wrapper walks the
host filesystem and calls OWUI REST **directly with the caller's user key**: the
caller creates + owns each project KB (`KB.user.email == caller`), so search
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
  `/home/owner/SOURCE/Deployments/knowledgebase` → encoded
  `-home-owner-SOURCE-Deployments-knowledgebase` → KB
  `mini2--home-owner-SOURCE-Deployments-knowledgebase`.
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
  done before relying on search.

`index-projects` is a full snapshot every run: it always re-uploads
`memory/*.md` (OWUI idempotency reuses unchanged files, no re-extract); a
**modified** file is delete-then-uploaded (router `DELETE` cleans the old
vectors — the upload's own reclaim does not); and orphans (source file gone)
are deleted so the KB mirror stays exact.

## Using the wrapper (`scripts/owui.py`, projects-memory subcommands)

```
KB=~/SOURCE/Deployments/knowledgebase
S=~/.pi/agent/skills/kb/scripts/owui.py
E="--env-file $KB/.env --env-file $KB/.env.local"   # KB_HOST in .env, key in .env.local

python3 "$S" $E index-projects --dry-run                  # plan only; show KB names + counts
python3 "$S" $E index-projects --project knowledgebase --wait   # index this repo, then wait for drain
python3 "$S" $E status-projects                           # current repo's drain status (walks up cwd)
python3 "$S" $E status-projects --json                    # machine-readable status dict
python3 "$S" $E search-projects "QPU scheduling"          # across ALL your project KBs
python3 "$S" $E search-projects "memory" --host mini2 --project knowledgebase   # filtered
python3 "$S" $E search-projects "XSL" --kb-glob 'mini2--*'                 # wildcard KB name
```

`search-projects` filters: `--host` (name starts with `<host>--`), `--project`
(substring in the project part), `--account` (KB owner email; default = caller,
aka `--mine`), `--kb-glob` (fnmatch on the KB name). No filters = all KBs you
own. It makes one retrieval call per KB (hit metadata carries no `knowledge_id`,
so one-call-per-KB is the reliable attribution) and prints
`repo=<repo> kb=<name> file=<file>` per hit.

# Facts memory (Graphiti, agent / kb-gateway)

Fact memory lives in Graphiti (Neo4j). Agents reach it **only** through the
`kb-gateway`, fronted by Caddy at **KB_HOST** under `/memory/*`. The gateway
derives your identity + role from your `KB_API_KEY` via Open WebUI (tamper-proof
— you cannot set your own identity). Authorization is server-side.

## Prerequisites

- The stack is up and healthy (`make start && make health`).
- You have a `KB_API_KEY` (an Open WebUI key). The admin's is
  `OPENWEBUI_ADMIN_API_KEY`; per-account keys are issued by the admin via
  `user-create` (below). Set `KB_HOST` (in `.env`; default
  `http://localhost:3000`) and `KB_API_KEY`, or pass `--env-file` (repeatable).
- For non-local `KB_HOST`, the URL must be HTTPS or a VPN/tunnel (`KB_API_KEY`
  is a bearer). On a trusted local interface plain HTTP is fine.

## Model

- **Writes go to your own personal group.** `add` with no `--group` writes to
  your personal group `user:<email>` (stored by Graphiti as `user-<sanitized-email>`,
  e.g. `user-agent-local-test`; `forget` accepts `user:<email>` too). `add --group G` is allowed only if `G` is
  your own personal group; any other group → `403`. There are no shared write
  groups — reads are how knowledge is shared across accounts.
- **Reads see ALL groups that have data, read-only.** `search` and `episodes`
  span every group the gateway discovers live from Neo4j (no roster file —
  future groups and out-of-band writes are included automatically).
- **Destructive ops require owning the target group or admin.** `forget`
  clears one group's memory (your own group, or admin). `delete-edge`/
  `delete-episode` delete one item by uuid: the gateway looks up the item's
  group and requires you own that group (or are admin); unknown uuids are
  rejected (fail-closed).
- **`status` is global** (Graphiti server + DB health); no group scoping.

## Using the wrapper (`scripts/kb_gateway.py`)

Zero-dependency (Python 3.8+ stdlib). One client for memory + admin ops. The
wrapper lives in `scripts/` next to this file; set `G` to its path in your
installed copy of this skill.

```
KB=~/SOURCE/Deployments/knowledgebase
G=~/.pi/agent/skills/kb/scripts/kb_gateway.py
E="--env-file $KB/.env --env-file $KB/.env.local"   # KB_HOST in .env, KB_API_KEY in .env.local

python3 "$G" $E whoami                                # verify identity (from the key, via the gateway)
python3 "$G" $E groups                                 # list all groups that have data
python3 "$G" $E add "Project Atlas uses a QPU scheduler" --name atlas
python3 "$G" $E search "QPU scheduling" --k 5          # facts across ALL groups (read-only)
python3 "$G" $E episodes --max 20                      # episodes across ALL groups (read-only)
python3 "$G" $E status                                 # graphiti server + DB status
python3 "$G" $E forget user:alice@example.com          # clear YOUR group's memory (owner/admin)
python3 "$G" $E delete-edge <uuid>                     # delete one edge (owner/admin of its group)
python3 "$G" $E delete-episode <uuid>                  # delete one episode (owner/admin of its group)
```

Errors: any non-200 from the gateway exits non-zero with the gateway's message
(401 = bad/missing key; 403 = not authorized for that op/group; 503 = identity
service down; 502 = graphiti/neo4j down; 501 = admin op unsupported by this
Open WebUI image).

# KB user provisioning (admin)

An admin can ask an agent to create a new KB user. The gateway enforces
`role=admin` **server-side** (a non-admin `KB_API_KEY` gets `403`), then runs
the full Open WebUI provisioning flow and returns the new user's email, a
generated temporary password, and their `KB_API_KEY`.

```
python3 "$G" $E user-create --email alice@example.com --name Alice
# admin KB_API_KEY only; prints: email, temp_password, kb_api_key, role, id
```

Rules:
- **Admin-only.** `KB_API_KEY` must resolve to an Open WebUI `admin`. Non-admin
  → `403`. Unsupported image (missing provisioning endpoints) → `501`.
- **What is returned:** `email`, `temp_password`, `kb_api_key`, `role`, `id`.
- **Relay to the requesting administrator ONLY.** Do NOT persist the
  returned `temp_password` or `kb_api_key` anywhere (the gateway never persists
  them; they exist only in this one response). The admin hands them to the new
  account out-of-band.
- **Rollback guarantee:** if the user is created but key generation or
  verification fails, the gateway deletes the partial user and returns a clear
  error — it never reports success for a half-provisioned account.
- **Duplicate email** returns a deterministic error (no second account).

## Triggering this skill

In Pi, a skill registers as a slash command `/skill:<name>` — so `/skill:kb`
deterministically loads this skill. Pi also auto-discovers skills by their
`SKILL.md` frontmatter (`description`) on natural phrasing. Both "KB" and
"knowledge base" phrasings are recognized.

| Trigger | Example phrasing | Deterministic? |
|---|---|---|
| Slash command | `/skill:kb` then the request | yes |
| Natural — search | "search the KB for X" / "search the knowledge base for X" | by description match |
| Natural — RAG chat | "ask the KB: \<question\>" / "ask the knowledge base: \<question\>" | by description match |
| Natural — list | "list my KBs" / "list my knowledge bases" | by description match |
| Natural — index projects memory | "index projects memory" / "index my Claude project memory" / "index project memory into KBs" | by description match |
| Natural — projects status | "projects memory index status" / "is my repo indexed" / "index status for this repo" | by description match |
| Natural — search projects memory | "search projects memory for X" / "search across my project KBs for X" | by description match |
| Natural — remember | "remember that …" / "what do we know about …" / "forget …" | by description match |
| Natural — create user | "create a new KB user \<email\> named …" (admin only) | by description match |

## Install location

This skill is installed at `~/.pi/agent/skills/kb/` (the `scripts/` directory
above is `~/.pi/agent/skills/kb/scripts/`). Pi auto-discovers skills there by
their `SKILL.md` frontmatter (`name` + `description`); `~/.agents/skills/kb/`
and project `.pi/skills/kb/` are also searched, and a skill can be loaded with
`--skill <path>` or listed in the `skills` array in `settings.json`.