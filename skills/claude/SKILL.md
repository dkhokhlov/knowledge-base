---
name: kb
description: Use when the user wants to query or chat with a self-hosted Open WebUI knowledge base (KB) over REST, index/search/retrieve Claude projects memory (~/.claude/projects/*/memory), or remember/search/retrieve Graphiti facts memory. Triggers on "KB"/"knowledge base", "index projects memory", "search projects memory"/"retrieve projects memory", "projects/repo index status", "remember …", "what do we know about …", and "forget …". One URL (KB_HOST) fronts OWUI REST (/api/*) and kb-gateway memory (/memory/*). Authenticates with KB_API_KEY (an Open WebUI key; read-scoped for KBs the caller does not own, write-scoped for the caller's own project KBs; identity+role derived server-side for facts memory). Zero-dependency Python CLI wrappers: scripts/owui.py (OWUI KBs + projects memory), scripts/kb_gateway.py (Graphiti facts).
---

# KB + memory (agent / kb-gateway)

Drive a self-hosted Open WebUI knowledge base over REST with the **agent
(user-role) API key**. This skill is read-only against KBs you do not own and
writes only the project KBs your key creates and owns. File upload, KB
create/delete, access grants, and admin config are out of scope (see
[Admin surface](#admin-surface)).

The stack is fronted at one URL, **KB_HOST**. OWUI REST is at the KB_HOST root
(`/api/*`); the kb-gateway memory endpoints are at `/memory/*` on the same host.
One URL, one key.

## Prerequisites

- A running, healthy Open WebUI + kb-gateway you can reach.
- **KB_HOST** — required, no default (e.g. `http://localhost:3000` for a local
  stack). The wrapper exits if it is unset.
- **KB_API_KEY** — an Open WebUI agent key with read grants on the KBs you
  query (write scope on your own project KBs). The wrapper exits if it is unset.
- For RAG chat, a strict RAG template (answer only from KB context) and a synced
  embedding URL must be configured server-side — without them the model falls
  back to its own knowledge and confabulates. That server-side setup is operator
  work, not part of this skill.
- Set both in your shell env: `export KB_HOST=...` / `export KB_API_KEY=...`.
  The wrapper is a thin client: it reads ONLY those two env vars and no env files.

## Auth

- Header: `Authorization: Bearer $KB_API_KEY`.
- The key belongs to a `user`-role (non-admin) account; run `whoami` to see its
  email.
- Read scope: sees KBs via their `*` (public) read grants; `write_access=false`
  on every KB it does not own. It cannot `file/add`, remove files, or delete a KB.

## Agent endpoints

Two ways to query a KB — **default to `retrieve`**: read the raw chunks and
synthesize the answer yourself. Use `rag` only for a quick one-shot answer when
the local model is adequate and the token cost of returning chunks matters.

### Retrieve (default)

`POST /api/v1/retrieval/query/collection` — body
`{collection_names:[<kb-id>], query, k, hybrid:true}` → Chroma
`{documents, distances, metadatas}` (nested arrays; the wrapper flattens
them; OWUI omits the Chroma `ids` array, so chunk ids are unavailable — each
chunk is identified by `file_id` + `start_index` in its metadata). Pure vector
retrieval — no LLM call. Lower distance = better match
(cosine; 0 best). Wrapper: `retrieve <kb-name-or-id> "<query>" [--k N] [--no-hybrid]`
(`--k` default 5; use 10–20 for broader recall — the agent synthesizes from
raw chunks, so more chunks serve it better than a one-shot `rag`.
`--no-hybrid` = pure vector, no hybrid search). The wrapper
resolves the name to a KB id via `GET /api/v1/knowledge/` (exact name or exact
id; a valid UUID that is not a real id FAILS — no silent fallthrough, so a
wrong hand-copied id cannot query the wrong KB) and prints the resolved
`kb_id` + `kb_name` alongside the hits.

- **To confirm a specific file is searchable**, retrieve by its **literal
  filename stem** with a higher `--k` (e.g. 20). A generic concept query can be
  outranked by topically-similar documents and report not-found even after the
  file is fully indexed and extracted. The filename stem is the discriminator
  that ranks the target file first.

### Chat (RAG)

`POST /memory/rag` (via the kb-gateway) — body
`{messages, files:[{type:collection,id:<kb-id>}]}` (NO `model` field) →
`{content}`. The gateway inserts the chat model server-side and forwards your
`KB_API_KEY` to OWUI, which enforces KB read access natively.

- Requires the server-side RAG config above; otherwise the model confabulates.
- Pass the KB via the top-level `files` field only. Do NOT use a `knowledge`
  field (silently ignored) or `metadata.knowledge` (discarded server-side).
  `type:collection` = whole-KB vector search; `type:file` = one file id.
- Wrapper: `rag "<question>" --kb <kb-id> [--kb <id2>]`.

### Discovery and file content

| Method | Path | Body / qs | Returns |
|---|---|---|---|
| GET | `/api/v1/auths/` | — | whoami: `{email, role}` |
| GET | `/api/v1/knowledge/` | — | `{items:[{id,name,description,file_count,write_access}]}` |
| GET | `/api/v1/knowledge/{id}` | — | one KB metadata |
| GET | `/api/v1/knowledge/search` | `?query=<text>` | KB name search |

## Phone-home (retrieval is safe)

Open WebUI builds the chromadb client with `anonymized_telemetry=False`. No
outbound telemetry on retrieve/chat.

## Admin surface

This skill is agent-scoped only. The full API (file upload, KB create/delete,
file bind/remove, access grants, user/admin config, retrieval processing) is
documented elsewhere: `GET /openapi.json` (JSON; no auth) and `/api/docs`
(interactive Swagger). Use an admin-role Open WebUI key for those — keep it
private; do not hand it to agents.

## Using the wrapper (`scripts/owui.py`)

Zero-dependency (Python 3.10+ stdlib). Set `S` to its path in your installed
skill (default `~/.claude/skills/kb/scripts/owui.py`).

```
S=~/.claude/skills/kb/scripts/owui.py
export KB_HOST=http://localhost:3000   # your stack URL (required)
export KB_API_KEY=...                  # your Open WebUI agent key (required)

python3 "$S" whoami                    # verify key + role
python3 "$S" kbs                       # list KBs visible to this key
python3 "$S" kb <kb-id>                 # one KB's metadata
python3 "$S" search-kbs "main"         # find a KB by name
python3 "$S" retrieve <kb-name-or-id> "XSL streaming"   # raw chunks, you synthesize (default)
python3 "$S" rag "What is XSL?" --kb <kb-id>     # one-shot RAG answer (via kb-gateway)
python3 "$S" file <file-id>             # file text content
```

Typical flow: `kbs` → `retrieve <kb-name>` (resolves name→id; fails loudly on
no-match; output includes the resolved `kb_id` + `kb_name`).

# Projects memory (Claude project memory → OWUI KBs)

**Projects memory** = Claude's per-project auto-memory
(`~/.claude/projects/<encoded-dir>/memory/*.md`), indexed into OWUI KBs — one
KB per project — so an agent can recall knowledge accumulated across Claude
Code projects and sessions. (Distinct from [Facts memory](#facts-memory-graphiti-kb-gateway)
below, which is the Graphiti knowledge graph.) The wrapper walks the host
filesystem and calls OWUI REST **directly with the caller's user key**: the
caller creates + owns each project KB (`KB.user.email == caller`), so
`retrieve-projects` filters KBs by owner. The kb-gateway is not involved.

## One-time setup

OWUI gates KB creation on the `workspace.knowledge` permission, which is off by
default. An admin must enable it once, then never again. `index-projects` fails
with a clear message if this has not been done.

## KB naming + metadata

- KB name = `<host>--<encoded-dir-without-leading-dash>`. Host = short hostname
  (`platform.node()`). Example: project `/home/user/projects/myrepo` → encoded
  `-home-user-projects-myrepo` → KB `<host>--home-user-projects-myrepo`.
- Per-file metadata (flat in `File.meta.data`): `host`, `project` (exact encoded
  dir), `project_path` (decode; authoritative when the path exists on disk, else
  lossy), `repo` (git repo name = path basename), `account` (caller email),
  `source_relpath` (`memory/<file>`). `repo` is the human-friendly identifier
  for reasoning about hits; it also rides in the KB `description`.

## Workflow

- **At session start**, run `index-projects` so this session's accumulated
  projects memory is in the KB and searchable across sessions/repos.
- **On an explicit user prompt** ("index projects memory"), re-run
  `index-projects` to refresh the KB for another repo's session.
- After indexing, run `status-projects` to confirm the current repo's drain is
  done before relying on retrieval.

`index-projects` is a full snapshot every run: it re-uploads `memory/*.md`
(OWUI idempotency reuses unchanged files, no re-extract); a **modified** file is
delete-then-uploaded (router `DELETE` cleans the old vectors); and orphans
(source file gone) are deleted so the KB mirror stays exact.

## Wrapper subcommands (projects memory)

```
S=~/.claude/skills/kb/scripts/owui.py   # KB_HOST + KB_API_KEY already exported

python3 "$S" index-projects --dry-run                 # plan only; JSON {projects,total}
python3 "$S" index-projects --project myrepo --wait   # index a repo, then wait for drain
python3 "$S" index-projects --root ~/.claude/projects # override the projects root
python3 "$S" status-projects                          # current repo's drain status (walks up cwd)
python3 "$S" status-projects --wait                   # poll until pending+processing == 0
python3 "$S" retrieve-projects "QPU scheduling"        # across ALL your project KBs
python3 "$S" retrieve-projects "memory" --host <host> --project myrepo  # filtered
```

`index-projects`: `--dry-run`, `--project <name>` (select; overrides cwd
walk-up), `--root <dir>` (default `~/.claude/projects`), `--no-cleanup` (do not
delete KB files whose source is gone), `--wait` (poll until the drain completes;
600s deadline).

`status-projects`: `--project <name>`, `--host <seg>` (override the cwd walk-up
defaults), `--wait`.

`retrieve-projects`: `--host` (name starts with `<host>--`), `--project`
(substring in the project part), `--account` (KB owner email, or fnmatch glob
like `*@corp.com` / `*` for all visible; default = caller, aka `--mine`),
`--kb-glob` (fnmatch on the KB name), `--k` (default 5), `--no-hybrid`. No
filters = all KBs you own. It makes one retrieval call per KB (hit metadata
carries no `knowledge_id`, so one-call-per-KB is the reliable attribution) and
prints compact JSON `{"kbs":N,"hits":[...],"errors":[...]}`.

Note: "search projects memory for X" maps to `retrieve-projects` (semantic). Do
not reach for `search-kbs` (KB-name lexical lookup only).

# Facts memory (Graphiti, kb-gateway)

Fact memory lives in Graphiti (Neo4j). Agents reach it **only** through the
kb-gateway at **KB_HOST** under `/memory/*`. The gateway derives your identity
+ role from your `KB_API_KEY` via Open WebUI (tamper-proof — you cannot set your
own identity). Authorization is server-side.

## Prerequisites

- A running, healthy kb-gateway at **KB_HOST** under `/memory/*`.
- **KB_HOST** and **KB_API_KEY** set in your shell env (same two vars as above;
  the wrapper reads only those). For non-local `KB_HOST`, the URL must be HTTPS
  or a VPN/tunnel (`KB_API_KEY` is a bearer). On a trusted local interface plain
  HTTP is fine.

## Model

- **Writes go to your own personal group.** `add` with no `--group` writes to
  your personal group `user:<email>` (stored by Graphiti as
  `user-<sanitized-email>`, e.g. `user:alice@example.com` → `user-alice-example-com`;
  `forget` accepts `user:<email>` too). `add --group G` is allowed only if `G`
  is your own personal group; any other group → `403`. There are no shared write
  groups — reads are how knowledge is shared across accounts.
- **Reads see ALL groups that have data, read-only.** `retrieve` and `episodes`
  span every group the gateway discovers live from Neo4j (no roster file —
  future groups and out-of-band writes are included automatically).
- **Destructive ops require owning the target group or admin.** `forget`
  clears one group's memory (your own group, or admin). `delete-edge` /
  `delete-episode` delete one item by uuid: the gateway looks up the item's
  group and requires you own that group (or are admin); unknown uuids are
  rejected (fail-closed).
- **`status` is global** (Graphiti server + DB health); no group scoping.

## Using the wrapper (`scripts/kb_gateway.py`)

Zero-dependency (Python 3.10+ stdlib). Set `G` to its path in your installed
skill (default `~/.claude/skills/kb/scripts/kb_gateway.py`).

```
G=~/.claude/skills/kb/scripts/kb_gateway.py   # KB_HOST + KB_API_KEY already exported

python3 "$G" whoami                              # verify identity (from the key, via the gateway)
python3 "$G" groups                              # list all groups that have data
python3 "$G" add "Project Atlas uses a QPU scheduler" --name atlas
python3 "$G" retrieve "QPU scheduling" --k 5      # facts across ALL groups (read-only; --k default 10)
python3 "$G" episodes --max 20                   # episodes across ALL groups (read-only; --max default 10)
python3 "$G" status                              # graphiti server + DB status
python3 "$G" forget user:<your-email>            # clear YOUR group's memory (owner/admin)
python3 "$G" delete-edge <uuid>                  # delete one edge (owner/admin of its group)
python3 "$G" delete-episode <uuid>               # delete one episode (owner/admin of its group)
```

**`add` is asynchronous:** Graphiti extracts entity edges in a background
Ollama pass after `add` returns, so an immediate `retrieve` for the just-added
fact can return `[]`. Wait, or retry, before treating a 0-hit retrieve as "not
remembered" — latency varies by host/model (warm seconds; a cold model load can
exceed a minute).

Errors: any non-200 from the gateway exits non-zero with the gateway's message
(401 = bad/missing key; 403 = not authorized for that op/group; 503 = identity
service down; 502 = graphiti/neo4j down; 501 = admin op unsupported by this
Open WebUI image).

# Triggering this skill

In Claude Code, the slash command (`/kb`) is the deterministic trigger.
Natural phrasing also triggers it when it matches the skill description above
("KB"/"knowledge base", "index/search/retrieve projects memory", "remember …",
"what do we know about …", "forget …").

## Install location

`~/.claude/skills/kb/` (scripts at `~/.claude/skills/kb/scripts/`). Claude Code
auto-discovers skills there by their `SKILL.md` frontmatter.