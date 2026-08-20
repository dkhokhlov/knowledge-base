# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [v1.1.0] — 2026-08-20

Graphiti memory is now functional with Ollama. v1.0.0's memory stack never
extracted facts (MCP transport + OpenAI Responses API client + wrong
endpoints). This release swaps to the Graphiti REST server with an injected
Ollama-compatible client, fixes the extraction footprint, and adds a
clean-state e2e harness.

### Major changes

- **Graphiti memory functional (MCP -> REST + client injection).** Replaced
  the `zepai/graphiti` MCP image (could not be exposed; its LLM client targets
  the OpenAI Responses API, which Ollama answers with no parseable entities
  -> extraction silently stored nothing) with
  `ghcr.io/dkhokhlov/graphiti-rest:0.29.3`. `gateway/mcp.py` (-180) ->
  `gateway/graphiti.py` (+149, stdlib urllib REST client). `graphiti/bootstrap.py`
  (+192) is mounted and run as the command; it overrides the FastAPI
  `get_graphiti` dependency to inject the stock `OpenAIGenericClient`
  (>= 0.29 defaults to `json_schema` structured outputs, enforced server-side
  by Ollama) at `temperature=0` + `OpenAIEmbedder(nomic-embed-text, 768)`,
  holds a process-level singleton (no per-request close, so the async
  `/messages` worker does not race a closed driver), and runs a robust worker
  loop that logs + isolates per-episode failures.
- **Extraction footprint fix (ctx-baked model).** The `/v1` endpoint ignores
  `num_ctx` in requests, so the context window is baked into the model via a
  Modelfile. New vars `OLLAMA_MODEL_BASE`, `OLLAMA_MODEL_CONTEXT` (8192),
  `MODEL_NAME=qwen2.5:14b-ctx8192`. The stock 14B at the default 32k loads
  ~53 GB and spills to CPU on a 22.5 GB GPU (extraction crawls / stalls);
  `num_ctx 8192` loads ~20 GB, fits the GPU; a fact is searchable in ~9 s warm
  (~30 s cold, model load).
  `OPENWEBUI_MODEL` must equal `MODEL_NAME` so only one 14B instance loads.
- **`make test-e2e` harness.** Destructive orchestrator: `clear-all` ->
  `bootstrap` -> restore creds -> `preflight` -> `start` -> health poll ->
  `admin-signup` -> `api-keys` -> `rag-config` -> `test`. Stashes / restores
  `OPENWEBUI_TEST_USER/PASSWORD` around the wipe; refuses (no wipe) if unset.
  New `make admin-signup` (idempotent OWUI admin signup) +
  `scripts/e2e-restore-creds.sh` (in-place KEY=VALUE restore, avoids a
  no-trailing-newline concatenation bug) + `tests/test_08_e2e.sh` (full
  gateway surface: whoami, status, add, search, delete-edge, delete-episode,
  forget, admin user-create + issued-key round-trip, non-admin deny).

### Operator-facing changes (action required on upgrade)

- **`OLLAMA_BASE_URL` -> `OLLAMA_HOST`** (Ollama's native client var). Set
  `OLLAMA_HOST` (full URL, e.g. `http://<ollama-host>:11434`) in your shell or `.env`;
  shell overrides `.env`. The Open WebUI container still receives
  `OLLAMA_BASE_URL` (the name OWUI reads on first boot), sourced from
  `${OLLAMA_HOST}`.
- **Model vars.** Add `OLLAMA_MODEL_BASE=qwen2.5:14b`,
  `OLLAMA_MODEL_CONTEXT=8192`, `MODEL_NAME=qwen2.5:14b-ctx8192`; set
  `OPENWEBUI_MODEL=qwen2.5:14b-ctx8192` (must equal `MODEL_NAME`). Run
  `make pull-models` (now pulls the base, creates the ctx variant via
  `PARAMETER num_ctx`, pulls the embedder) and `make restart`.
- **`GRAPHITI_IMAGE_TAG=0.29.3`** (was absent / MCP). If a differently-named
  14B variant is already loaded in Ollama with a long keep-alive, `ollama stop`
  + `ollama rm` the obsolete alias first, then recreate the graphiti container
  (the running process keeps its startup-time model name).

### Fixes

- **Group_id charset.** Graphiti rejects `user:<email>` (charset
  `[A-Za-z0-9_-]`). The gateway now sanitizes `user:<email>` ->
  `user-<sanitized-email>` on write and normalizes the boundary on forget;
  callers keep the logical `user:<email>` form.
- **LLM was hitting api.openai.com.** `compose.yml` now sets `OPENAI_BASE_URL`
  on the graphiti service so extraction reaches Ollama.
- **`clear-all` wipe.** OWUI (root) and Neo4j (neo4j uid) write bind-mount
  files the host user cannot delete, so `rm -rf ./data` aborted midway and left
  a stale `.env.local`. Now wipes `./data` as root via a throwaway `alpine`
  container so the wipe completes.
- **Reasoning-model failure mode.** `MODEL_NAME` must be non-reasoning; a
  reasoning model spends `max_tokens` on its thinking chain and emits no
  content -> `json.loads('')` -> silent extraction failure. Default is
  `qwen2.5:14b`.
- **Extraction-detection flakiness.** Numbers embedded as quantities in
  probes are non-deterministically rephrased out of fact text (even at
  `temperature=0`); tests now detect on the stable descriptive noun, not the
  run-id number.

### New

- **In-repo per-tool skills** under `skills/{claude,codex,opencode,pi}/`
  (per-tool `SKILL.md`; scripts symlink to `skills/claude/scripts`).
- `make preflight` verifies the model exists **and** its `num_ctx` matches
  `OLLAMA_MODEL_CONTEXT` (a wrong / absent value silently reverts to 32k /
  CPU spill).
- `docs/agents.md`; README + `docs/operations.md` rewritten for the REST
  backend, the injection, the num_ctx model lifecycle, and new
  troubleshooting rows (slow-extraction / CPU-spill, reasoning-model empty
  response).

### Tests

`make test-e2e` green from clean state: test_01..08, 59 assertions, 0 failed.

## [v1.0.0] — 2026-08-20

Initial tagged release. MCP-based Graphiti memory stack (memory extraction
non-functional with Ollama — fixed in v1.1.0).

[v1.1.0]: https://github.com/dkhokhlov/knowledgebase/releases/tag/v1.1.0
[v1.0.0]: https://github.com/dkhokhlov/knowledgebase/releases/tag/v1.0.0