# Testing

System integration tests in `tests/` exercise the running stack over HTTP.
For operations and configuration see [operations.md](operations.md); for the
architecture and trust model see the [README](../README.md).

## Prerequisites

The suite needs the stack up (`make start && make health`) and keys in
`.env.local` (see `.env.local.example`):

- `OPENWEBUI_TEST_USER` / `OPENWEBUI_TEST_PASSWORD` — an existing [Open WebUI][open-webui]
  user (e.g. the admin). `test_03` signs in to get a JWT.
- `OPENWEBUI_ADMIN_API_KEY` / `OPENWEBUI_USER_API_KEY` — admin + agent keys (valid `KB_API_KEY`s for the kb-gateway). Provisioned by `make api-keys`.

## Run the suite

```
make test
```

`make test` runs all `tests/test_*.sh` scripts and exits non-zero if any fail.

## Test matrix

| Script | Checks | Auth |
|---|---|---|
| `tests/test_01_health.sh` | kb-gateway `/health` (via Caddy), dev-mode `/openapi.json`, Neo4j not published, kb-gateway rejects a missing key (401) | none |
| `tests/test_02_gateway.sh` | kb-gateway read path: `whoami` (admin role from key), `groups` (Neo4j discovery), `search` (read-all facts), `episodes`, `status`; bad key → 401 | `OPENWEBUI_ADMIN_API_KEY` |
| `tests/test_03_openwebui_rest.sh` | `signin` → JWT, chat completion → [Ollama][ollama] `MODEL_NAME` | `OPENWEBUI_TEST_USER/PASSWORD` |
| `tests/test_04_openwebui_rag.sh` | RAG embedding URL reachable from container; upload → embed → bind → `/api/v1/retrieval/query/collection` returns the indexed doc | `OPENWEBUI_TEST_USER/PASSWORD` |
| `tests/test_05_openwebui_user_readonly.sh` | Agent (`OPENWEBUI_USER_API_KEY`) is role=user; reads + searches a `*`-granted KB (`write_access=False`); denied `file/add` and `delete`; admin key contrast has write access; RAG chat grounded via `files:[{type:collection,id}]` — unique marker present in the answer (catches a `knowledge`-field regression) | `OPENWEBUI_ADMIN_API_KEY`, `OPENWEBUI_USER_API_KEY` |
| `tests/test_06_gateway.sh` | kb-gateway authz matrix: identities from keys; agent personal write (200); read-all search+episodes (200); cross-user `forget` (403); spoof/claim another user's group (403); unknown shared group (403); admin override `forget` (200) | `OPENWEBUI_ADMIN_API_KEY`, `OPENWEBUI_USER_API_KEY` |
| `tests/test_07_admin_users.sh` | Admin user provisioning: create → email + temp_password + `kb_api_key` + role=user; returned key `whoami` → new user; non-admin → 403; duplicate → non-2xx; partial-failure rollback (isolated gateway + test-only flag) → error + partial user cleaned up | `OPENWEBUI_ADMIN_API_KEY`, `OPENWEBUI_USER_API_KEY` |
| `tests/test_08_e2e.sh` | kb-gateway end-to-end via `kb_gateway.py`: agent surface (`whoami`, `status`, `groups`, `add`, `search`, `episodes`, `delete-edge`, `delete-episode`, `forget`) + admin user-create (issued key resolves to the new user); non-admin `POST /admin/users` → 403. Covers `delete-edge`/`delete-episode` test_06 skips | `OPENWEBUI_ADMIN_API_KEY`, `OPENWEBUI_USER_API_KEY` |
| `tests/test_09_gdrive_index.sh` | gdrive auto-index: `kb-gdrive-indexer` up, oikb daemon ready, source sync status healthy, OWUI "gdrive" KB has files; best-effort semantic search (a hit is a bonus, not a hard requirement). SKIPs when `GDRIVE_KB_ID` is unset or `./gdrive` has no allowlisted files | `OPENWEBUI_USER_API_KEY` |
| `tests/test_10_ocr_auth.sh` | markitdown-ocr auth gate: the `kb-markitdown-ocr` container holds a non-empty `OCR_SERVICE_TOKEN` (stale-recreate guard), and `/process` rejects a request without a Bearer (401) and accepts the matching Bearer (200). SKIPs when `MARKITDOWN_OCR_PROVISIONED!=1` | none (token read from the container env) |

## Notes

- The tests are read-only except `test_03` (one stateless chat completion), `test_04` (creates a KB + file, then deletes both on exit), `test_05` (admin creates a temp KB + file + `*` read grant, then deletes all three on exit), `test_06` (writes to a temp agent group, then clears it), `test_07` (creates temp OWUI users, then deletes them via `DELETE /api/v1/users/{id}`), and `test_08` (creates a throwaway OWUI user + agent group via the gateway, then forgets the group + deletes the user).
- `test_07` rollback case starts an isolated kb-gateway (`docker compose run`) with the test-only `KB_TEST_PROVISION_FAIL_AFTER_CREATE` flag to force a failure after user creation; if that run mechanism is unavailable in the environment it is skipped (verify it exercises, not skips, on the host).
- `test_09` and `test_10` SKIP (pass with a notice) when their sidecar is not provisioned — `GDRIVE_KB_ID` unset / `./gdrive` empty (test_09) or `MARKITDOWN_OCR_PROVISIONED!=1` (test_10) — so `make test` runs clean in a bare environment. The full clean-state run that provisions every sidecar is `make test-e2e` (destructive: wipes `./data` + re-provisions).
- Each script sources `tests/lib.sh` (env loader, pass/fail counters, stack-up guard) and exits non-zero on any failure.

[open-webui]: https://github.com/open-webui/open-webui
[ollama]: https://ollama.com/