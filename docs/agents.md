# Agent integration

How an agent (Claude Code, Codex, OpenCode, Pi) connects to this knowledge stack.
One URL (`KB_HOST`), one key (`KB_API_KEY`), one skill (`kb`).

The stack exposes a single public URL, **`KB_HOST`** (default
`http://localhost:3000`). Caddy fronts Open WebUI at the root and the kb-gateway
under `/memory/*`, `POST /admin/users`, and `/health`. An agent holds only
`KB_API_KEY` + `KB_HOST` and works on any host that can reach `KB_HOST`.

| Surface | Path | Auth | Use |
|---|---|---|---|
| Open WebUI REST | `KB_HOST/api/*` | `Bearer <KB_API_KEY>` | chat, RAG, files, knowledge bases |
| kb-gateway memory | `KB_HOST/memory/*` | `Bearer <KB_API_KEY>` | Graphiti facts: whoami, groups, add, search, episodes, status, forget, delete-edge, delete-episode |
| kb-gateway admin | `KB_HOST/admin/users` (POST) | `Bearer <KB_API_KEY>` (admin) | create a new KB user (returns temp password + `KB_API_KEY`) |
| health | `KB_HOST/health` | none | read-only stack probe |

`KB_API_KEY` is an Open WebUI per-account API key. The admin key
(`OPENWEBUI_ADMIN_API_KEY`) grants admin role + override; the agent key
(`OPENWEBUI_USER_API_KEY`) is read-scoped for KBs. Per-account keys are issued by
the admin via `user-create`. `KB_API_KEY` is a bearer — `KB_HOST` MUST be HTTPS or
VPN/tunnel for any non-local agent.

## Prerequisites

- The stack is up and healthy: `make start && make health`.
- `KB_HOST` is set in `.env` (default `http://localhost:3000`). Replace `<host>` with the Docker host name/IP, or `localhost` if the client runs on the Docker host.
- You have a `KB_API_KEY`. For the bootstrap admin + read-scoped agent keys, run
  `make api-keys` (writes `OPENWEBUI_ADMIN_API_KEY` / `OPENWEBUI_USER_API_KEY` into
  gitignored `.env.local`). For additional accounts, an admin runs `user-create`.

## The `kb` skill

The repo ships the `kb` skill in `skills/<tool>/` (one per agent tool):
`skills/claude/` is the primary copy (real `SKILL.md` + real `scripts/`);
`skills/codex/`, `skills/opencode/`, `skills/pi/` each hold a per-tool `SKILL.md`
with `scripts/` symlinked to `../claude/scripts`. The wrappers are zero-dependency
Python 3.8+ stdlib:

- `scripts/owui.py` — Open WebUI REST (read-scoped): `whoami`, `kbs`, `search`,
  `rag`, `file`.
- `scripts/kb_gateway.py` — kb-gateway: memory (`whoami`, `groups`, `add`,
  `search`, `episodes`, `status`, `forget`, `delete-edge`, `delete-episode`) and
  admin (`user-create`).

Both read `KB_HOST` (fallback synth from `KB_HOST_PORT`) + `KB_API_KEY` (fallback
`OPENWEBUI_USER_API_KEY`) from the environment or `--env-file` (repeatable; load
`.env` then `.env.local`).

### Triggers

| Trigger | Example phrasing | Deterministic? |
|---|---|---|
| Slash command | `/kb` then the request | yes |
| Natural — search | "search the KB for X" / "search the knowledge base for X" | by description match |
| Natural — RAG chat | "ask the KB: \<question\>" / "ask the knowledge base: \<question\>" | by description match |
| Natural — list | "list my KBs" / "list my knowledge bases" | by description match |

The slash command is the most reliable; natural phrasing triggers automatically
when it matches the skill description. Both "KB" and "knowledge base" phrasings
are recognized.

### Install per tool

Install the matching `skills/<tool>/` directory into the tool's skill location,
then point the wrappers at `.env` / `.env.local`:

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

Set `E` once to load `KB_HOST` (`.env`) + `KB_API_KEY` (`.env.local`):

```
KB=~/SOURCE/Deployments/knowledgebase          # this repo
E="--env-file $KB/.env --env-file $KB/.env.local"
S=~/.claude/skills/kb/scripts                   # your tool's installed skill dir
```

### Verify identity

```
python3 "$S/owui.py"      $E whoami    # OWUI: email + role
python3 "$S/kb_gateway.py" $E whoami    # kb-gateway: email + role + id (derived from the key)
```

### Open WebUI KBs (read-scoped)

```
python3 "$S/owui.py" $E kbs                          # list visible KBs
python3 "$S/owui.py" $E search-kbs "main"            # find a KB by name
python3 "$S/owui.py" $E search <kb-id> "XSL streaming"   # raw chunks (you synthesize)
python3 "$S/owui.py" $E rag "What is XSL?" --kb <kb-id>  # RAG chat (LLM answer from the KB)
```

RAG chat needs `make rag-config` (strict-grounding template + synced embedding
URL); without it the model confabulates. Ground the chat via the top-level
`files` field only (`{"type":"collection","id":"<kb-id>"}`) — a `knowledge` field
is ignored.

### Graphiti memory (kb-gateway)

```
python3 "$S/kb_gateway.py" $E groups                              # list all groups that have data
python3 "$S/kb_gateway.py" $E add "Project Atlas uses a QPU scheduler" --name atlas
python3 "$S/kb_gateway.py" $E search "QPU scheduling" --k 5        # facts across ALL groups (read-only)
python3 "$S/kb_gateway.py" $E episodes --max 20                    # episodes across ALL groups (read-only)
python3 "$S/kb_gateway.py" $E status                               # graphiti server + DB status
python3 "$S/kb_gateway.py" $E forget user:<me>                     # clear YOUR group (owner/admin)
python3 "$S/kb_gateway.py" $E delete-edge <uuid>                   # delete one edge (owner/admin)
```

Model: writes go to your own personal group (logical `user:<email>`, stored by
Graphiti as `user-<sanitized-email>` e.g. `user-agent-local-test`; `forget`
accepts `user:<email>` too). `add --group G` is allowed only for your own group;
any other → `403`. Reads span all groups read-only. Destructive ops require
owning the target group or admin.

### Create a new KB user (admin only)

```
python3 "$S/kb_gateway.py" $E user-create --email alice@example.com --name Alice
# prints: email, temp_password, kb_api_key, role, id  — relay to the admin ONLY; do not persist
```

The gateway enforces `role=admin` server-side (non-admin → `403`) and rolls back
a partial user if key generation fails. The returned `temp_password` + `kb_api_key`
exist only in the one response — relay them to the requesting administrator
out-of-band and do not store them.

## Errors

Any non-200 exits non-zero with the gateway's message:

- `401` bad/missing `KB_API_KEY`
- `403` not authorized for that op/group (e.g. writing to another user's group)
- `501` admin op unsupported by the deployed Open WebUI image
- `502` graphiti/neo4j down
- `503` identity service (Open WebUI) down — fail-closed