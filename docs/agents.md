# Agent integration

How an agent (Claude Code, Codex, OpenCode, Pi) connects to this knowledge stack.
One URL (`KB_HOST`), one key (`KB_API_KEY`), one skill (`kb`).

The stack exposes a single public URL, **`KB_HOST`** (default
`http://localhost:3000`). Caddy fronts Open WebUI at the root and the api-gateway
under `/memory/*`, `POST /admin/users`, and `/health`. An agent holds only
`KB_API_KEY` + `KB_HOST` and works on any host that can reach `KB_HOST`.

| Surface | Path | Auth | Use |
|---|---|---|---|
| Open WebUI REST | `KB_HOST/api/*` | `Bearer <KB_API_KEY>` | files, knowledge bases, projects memory (Claude Code skill only; humans/admins also RAG directly here with an explicit `model`) |
| api-gateway memory | `KB_HOST/memory/*` | `Bearer <KB_API_KEY>` | Graphiti facts (whoami, groups, add, retrieve, episodes, status, forget, delete-edge, delete-episode) |
| api-gateway admin | `KB_HOST/admin/users` (POST) | `Bearer <KB_API_KEY>` (admin) | create a new KB user (returns temp password + `KB_API_KEY`) |
| health | `KB_HOST/health` | none | read-only stack probe |

`KB_API_KEY` is an Open WebUI per-account API key. The admin key
(`OPENWEBUI_ADMIN_API_KEY`) grants admin role + override; a non-admin user key
(`KB_API_KEY`, from `make users-create`) is read-scoped for KBs. Per-account keys
are issued by the admin via `make users-create`. `KB_API_KEY` is a bearer — `KB_HOST`
MUST be HTTPS or VPN/tunnel for any non-local agent.

## Prerequisites

- The stack is up and healthy: `make start && make health`.
- `KB_HOST` is set in your shell env (mandatory, no default — `export KB_HOST=http://<host>:3000`; `make bootstrap` persists it into `.env`). Replace `<host>` with the Docker host name/IP, or `localhost` if the client runs on the Docker host.
- You have a `KB_API_KEY`. For the bootstrap admin key, run
  `make api-keys` (writes `OPENWEBUI_ADMIN_API_KEY` into
  gitignored `.env.local`). For your own non-admin user key, run
  `make users-create EMAIL=... NAME=...` (admin key required; it prints `kb_api_key`
  to relay) and store it in `~/.api_keys` as `KB_API_KEY`.

## The `kb` skill

The repo ships the `kb` skill in `skills/<tool>/` (one per agent tool):
`skills/claude/` is the primary copy (real `SKILL.md` + real `scripts/`);
`skills/codex/`, `skills/opencode/`, `skills/pi/` each hold a per-tool `SKILL.md`
with `scripts/` symlinked to `../claude/scripts`. The wrappers are zero-dependency
Python 3.10+ stdlib:

- `scripts/kb.py` — one self-contained CLI. **Top level:** Open WebUI REST KB
  surface (read-scoped) `whoami`, `kbs`, `retrieve`, `file` (`kbs` surfaces each
  KB's `description` + the parsed source attribute — `source`/`host`/`path`/
  `project`/`repo` — read from the description kv); **projects memory**
  (user-key writes to owned KBs) `index-projects`, `retrieve-projects`,
  `status-projects` — **Claude Code skill copy only**; the shared `kb.py` keeps the
  subcommands, but the codex/opencode/pi `SKILL.md` copies do not document them.
  **`memory` subcommand:** api-gateway facts memory (`whoami`, `groups`, `add`,
  `retrieve`, `episodes`, `status`, `forget`, `delete-edge`, `delete-episode`).

It reads ONLY `KB_HOST` + `KB_API_KEY` from the shell environment — no
`.env` / `.env.local` files, no `--env-file`, no other env vars. Set both in
your shell before invoking them (`export KB_HOST=...` / `export KB_API_KEY=...`).

### Triggers

| Trigger | Example phrasing | Deterministic? |
|---|---|---|
| Slash command | `/kb` then the request | yes |
| Natural — search | "search the KB for X" / "search the knowledge base for X" | by description match |
| Natural — list | "list my KBs" / "list my knowledge bases" | by description match |
| Natural — index projects memory | "index projects memory" / "index my Claude project memory" | by description match (Claude Code only) |
| Natural — projects status | "projects memory index status" / "is my repo indexed" | by description match (Claude Code only) |
| Natural — search projects memory | "search projects memory for X" / "search across my project KBs for X" | by description match (Claude Code only) |

The slash command is the most reliable; natural phrasing triggers automatically
when it matches the skill description. Both "KB" and "knowledge base" phrasings
are recognized.

### Install per tool

Install the matching `skills/<tool>/` directory into the tool's skill location,
then set `KB_HOST` + `KB_API_KEY` in your shell env:

| Tool | Install location | Trigger |
|---|---|---|
| Claude Code | `~/.claude/skills/kb/` | `/kb` slash command + natural language |
| Codex | `~/.codex/skills/kb/` (also `~/.agents/skills/kb/`) | `$kb` mention + description auto-discovery |
| OpenCode | `~/.config/opencode/skills/kb/` (also `~/.claude/skills/kb/`, `.opencode/skills/kb/`) | auto-discovery by description |
| Pi | `~/.pi/agent/skills/kb/` (also `~/.agents/skills/kb/`) | `/skill:kb` slash command + description auto-discovery |

All four tools use the same `SKILL.md` frontmatter (`name: kb` + `description`).

### Deploying to a host (follow the symlinks)

`skills/<tool>/scripts` is a **relative symlink** to `../claude/scripts`. When you
copy a single tool's skill to a host, the `../claude/scripts` target does not
exist at the destination, so the symlink breaks. **Dereference** it so the
wrappers become real files:

```
# copy one tool's skill to a host, following the scripts symlink into real files:
cp -Lr skills/claude/ ~/.claude/skills/kb/
# or with rsync / tar:
rsync -Lr skills/claude/ <host>:~/.claude/skills/kb/
tar -h -cf - skills/claude | tar -xf - -C ~/.claude/skills/ && mv ~/.claude/skills/claude ~/.claude/skills/kb
```

(`cp -L` / `rsync -L` / `tar -h` follow symlinks and copy the referenced files.)
The `SKILL.md` in each `skills/<tool>/` is a real file (it differs per tool), so it
copies normally.

## Example flows

Export `KB_HOST` + `KB_API_KEY` once (the wrappers read only those two):

```
export KB_HOST=http://localhost:3000            # or your KB_HOST
export KB_API_KEY=...                           # your own user key (make users-create; ~/.api_keys)
KB=~/.claude/skills/kb/scripts/kb.py          # your tool's installed wrapper
```

### Verify identity

```
python3 "$KB" whoami              # OWUI: email + role
python3 "$KB" memory whoami       # api-gateway: email + role + id (derived from the key)
```

### Open WebUI KBs (read-scoped)

```
python3 "$KB" kbs                          # list visible KBs
python3 "$KB" search-kbs "main"            # find a KB by name
python3 "$KB" retrieve <kb-id> "XSL streaming"   # raw chunks (you synthesize)
```

### Projects memory (kb.py, user key) — Claude Code skill only

Available only in the Claude Code skill copy (`skills/claude/SKILL.md`); the
codex/opencode/pi copies do not expose projects memory. The shared `kb.py`
keeps the subcommands.

**Projects memory** = Claude's per-project auto-memory
(`~/.claude/projects/<encoded-dir>/memory/*.md`), indexed into OWUI KBs — one KB
per project — so an agent recalls knowledge across Claude Code projects and
sessions. The wrapper walks the host filesystem and calls OWUI REST directly
with the caller's user key, which creates + owns each project KB
(`KB.user.email == caller`). The api-gateway is not involved.

**One-time setup** (admin): `make projects-bootstrap` enables the
`workspace.knowledge` permission (off by default) so the user key can create
KBs. `index-projects` fails with a clear message until this is run.

KB name = `<host>--<encoded-dir-without-leading-dash>` (host = short hostname).
Per-file metadata: `host`, `project`, `project_path`, `repo` (git repo name),
`account`, `source_relpath`, `mtime` (file mtime, ISO-UTC; only new/changed
files — idempotency reuses unchanged files without touching `File.meta`). The
KB `description` carries the source-attribute kv (`source=projects-memory |
host=.. | project=.. | repo=.. | path=..`); `kbs` parses it, so hits are easy
to reason about (`retrieve-projects` returns compact JSON
`{"kbs":N,"hits":[{"repo","kb_name","file","text"}],"errors":[...]}`).

```
python3 "$KB" index-projects --dry-run                  # plan only
python3 "$KB" index-projects --project knowledgebase --wait   # index this repo, wait for drain
python3 "$KB" status-projects                           # current repo's drain status (walks up cwd)
python3 "$KB" retrieve-projects "QPU scheduling"          # across ALL your project KBs
python3 "$KB" retrieve-projects "X" --host <host> --project knowledgebase   # filtered
```

**Workflow**: run `index-projects` at session start (so this session's memory is
searchable across sessions/repos) and on an explicit "index projects memory"
prompt; then `status-projects` to confirm the current repo's drain before
relying on retrieval — same "refresh at session start" convention as
open-codebase-index. `index-projects` is a full snapshot every run (always
re-uploads `memory/*.md`; OWUI idempotency reuses unchanged files; a modified
file is delete-then-uploaded so no stale vectors; orphans are deleted).

### Facts memory (Graphiti, api-gateway)

```
python3 "$KB" memory groups                              # list all groups that have data
python3 "$KB" memory add "Project Atlas uses a QPU scheduler" --name atlas
python3 "$KB" memory retrieve "QPU scheduling" --k 5        # facts across ALL groups (read-only)
python3 "$KB" memory episodes --max 20                    # episodes across ALL groups (read-only)
python3 "$KB" memory status                               # graphiti server + DB status
python3 "$KB" memory forget user:<me>                     # clear YOUR group (owner/admin)
python3 "$KB" memory delete-edge <uuid>                   # delete one edge (owner/admin)
```

Model: writes go to your own personal group (logical `user:<email>`, stored by
Graphiti as `user-<sanitized-email>` e.g. `user-agent-local-test`; `forget`
accepts `user:<email>` too). `add --group G` is allowed only for your own group;
any other → `403`. Reads span all groups read-only. Destructive ops require
owning the target group or admin.

### Create a new KB user (admin only)

User provisioning is an operator make target (not an in-skill command):

```
make users-create EMAIL=alice@example.com NAME=Alice
# prints pretty JSON: email, temp_password, kb_api_key, role, id  — relay to the
# new account out-of-band ONLY; do not persist
```

`make users-list` and `make users-search QUERY=<q>` list/search users (pretty
JSON). The make target calls the api-gateway `POST /admin/users` flow: the gateway
enforces `role=admin` server-side (non-admin → `403`) and rolls back a partial
user if key generation fails. The returned `temp_password` + `kb_api_key` exist
only in the one response — relay them to the new account out-of-band and do not
store them.

## Errors

Any non-200 exits non-zero with the gateway's message:

- `401` bad/missing `KB_API_KEY`
- `403` not authorized for that op/group (e.g. writing to another user's group)
- `501` admin op unsupported by the deployed Open WebUI image
- `502` graphiti/neo4j down
- `503` identity service (Open WebUI) down — fail-closed