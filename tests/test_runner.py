#!/usr/bin/env python3
"""Pytest test runner: one test function per bash test script.

Each function wraps one tests/test_*.sh and runs it
via a fixture (tests/conftest.py). A test passes iff its script exits 0 (lib.sh
``finish`` exits non-zero on FAIL>0; ``require_stack_up`` exits 2 when the stack
is down). The script inherits this process's fds (live stack) OR runs in a clean
child env (iso), so output streams live (not buffered until the end).

Markers are on each test, in-file:
  integration  live stack read-only system test (run: make start first)
  iso          isolated test against a throwaway clean-prod stack (session-shared
               via iso_env, or a named own-iso stack via iso_env_named;
               scripts/lib-e2e-env.sh)
  shared       cross-cutting tag on the iso tests that share ONE session stack
  long         cross-cutting tag (real-corpus drain or at-scale); stacked with the
               primary group (a test can be iso AND long)

Fixtures (tests/conftest.py):
  run_sh        live stack (integration, test_01/02/03).
  iso_env       session-shared clean-prod stack (iso+shared, 10 tests).
  iso_env_named a named own-iso stack (iso+long / iso single).

The python unit tests are separate: native stdlib unittest.TestCase modules
(tests/test_gateway_unit.py, test_kb_check.py, test_offset_aware_chunking.py,
test_output_json.py), marked ``unit`` by tests/conftest.py. They are NOT wrapped
here; pytest collects them natively.

Make targets: make test | test-unit | test-live-RO | test-iso | test-long.
(See tests/README.md for the full tier -> marker -> target table.)
"""

import pytest

@pytest.mark.integration
def test_01_health(run_sh):
    """Stack liveness, dev-mode docs, Neo4j exposure, and the api-gateway auth gate (via Caddy). No auth required."""
    run_sh("tests/test_01_health.sh")

@pytest.mark.integration
def test_02_gateway(run_sh):
    """api-gateway read path (Caddy -> api-gateway -> graphiti -> Neo4j) with the admin key: whoami, groups, retrieve, episodes, status. Read-only."""
    run_sh("tests/test_02_gateway.sh")

@pytest.mark.integration
def test_03_openwebui_rest(run_sh):
    """Open WebUI REST auth + chat completion (signin/JWT path + Open WebUI -> Ollama LLM path)."""
    run_sh("tests/test_03_openwebui_rest.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_04_openwebui_rag(iso_env):
    """Open WebUI RAG embedding endpoint + upload/embed/search. Regression guard for the persisted rag.ollama.base_url stale-host failure."""
    iso_env("tests/test_04_openwebui_rag.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_05_openwebui_user_readonly(iso_env):
    """The non-admin user API key is read-scoped (user-key KB create via workspace.knowledge; temp KB, '*' read grant, write/delete deny assertions)."""
    iso_env("tests/test_05_openwebui_user_readonly.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_06_gateway(iso_env):
    """api-gateway authorization matrix (admin + non-admin user keys; personal write, read-all, cross-user deny, admin override, spoof/claim deny, shared-group)."""
    iso_env("tests/test_06_gateway.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_07_admin_users(iso_env):
    """Admin-driven KB user provisioning via the api-gateway (POST /admin/users; create, whoami, non-admin 403, duplicate, partial-failure rollback)."""
    iso_env("tests/test_07_admin_users.sh")

@pytest.mark.iso
@pytest.mark.long
def test_08_e2e(iso_env_named):
    """Isolated e2e: api-gateway Graphiti agent surface (whoami/status/groups/add/retrieve/episodes/delete-edge/delete-episode/forget) against a named throwaway stack (fixture owns isolate+provision+teardown). LONG."""
    iso_env_named("test08")("tests/test_08_e2e.sh")

@pytest.mark.iso
@pytest.mark.long
def test_09_gdrive_index(iso_env_named):
    """Comprehensive at-scale e2e (iso long): clean-all + image rebuild + preflight + real rclone gdrive corpus + in-clone suite (unit + live-RO 01/02/03) + the full real-gdrive drain (POST /index + poll GET /status + failure audit + semantic search) on a named throwaway stack (fixture owns isolate + at-scale provision + teardown). LONG."""
    iso_env_named("gdrive", at_scale=True)("tests/test_09_gdrive_index.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_10_ocr_auth(iso_env):
    """markitdown-ocr /process auth gate (token wired end-to-end; 401 without Bearer, 200 with). Tolerant SKIP when OCR disabled."""
    iso_env("tests/test_10_ocr_auth.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_11_gdrive_index_fixture(iso_env):
    """api-gateway /index `path` parameter on a small, deterministic, committed fixture set (fast replacement for the full test_09 drain)."""
    iso_env("tests/test_11_gdrive_index_fixture.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_13_chunk_quality(iso_env):
    """Chunk-QUALITY audit over generated fixtures for all 10 allowlisted types (sliceability, span/page metadata, coalescing, offsets, fidelity)."""
    iso_env("tests/test_13_chunk_quality.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_14_rag_config(iso_env):
    """rag-config.sh re-asserts the hybrid retrieval keys over webui.db and OWUI runs the pgvector backend."""
    iso_env("tests/test_14_rag_config.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_15_retrieve(iso_env):
    """gateway-mediated POST /retrieve: Caddy route + validation matrix + all three modes 200 + lexical exact-token acceptance (pgvector FTS)."""
    iso_env("tests/test_15_retrieve.sh")

@pytest.mark.iso
def test_12_kb_check(iso_env_named):
    """Isolated e2e for make kb-check on pgvector: named throwaway stack (fixture owns isolate+provision+teardown), create a reproducible leak class, detect -> PURGE=1 export+purge -> re-audit 0."""
    iso_env_named("kbcheck")("tests/test_12_kb_check.sh")


@pytest.mark.iso
def test_16_generic_kb(iso_env_named):
    """Generic (non-gdrive) KB under ./root/<name>/: the additive .kb-ignore ancestor-chain deny-list (root/.kb-ignore globals + per-KB root/<name>/.kb-ignore, with a `!` re-include) on a non-gdrive KB + the generic shell pipeline (make kb-bootstrap/kb-index/kb-finalize by name). Named own-iso stack (REINDEX is instance-wide)."""
    iso_env_named("gentest")("tests/test_16_generic_kb.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_17_gdrive_meta_sidecar(iso_env):
    """gdrive `.meta.json` sidecar -> File.meta.data.gdrive -> kb skill retrieve gdrive-join, e2e on a real stack: index a committed fixture set (docs + sidecars + one no-sidecar control) and assert each hit's joined gdrive record (grounded/labels/approval) over the kb skill `retrieve` path."""
    iso_env("tests/test_17_gdrive_meta_sidecar.sh")

@pytest.mark.iso
@pytest.mark.shared
def test_18_projects_exclude(iso_env):
    """index-projects honors <root>/.kb-ignore (gitignore-style allowlist) e2e on a real stack: throwaway projects root with 3 project dirs + `.kb-ignore` (`*` + `!allowA` + `!allowB`) -> real `index-projects --root <fixture> --host test18h --wait` (kb skill, user key) -> assert only allowA + allowB selected/created (denyC excluded), and allowA's marker is retrievable (drain landed vectors)."""
    iso_env("tests/test_18_projects_exclude.sh")