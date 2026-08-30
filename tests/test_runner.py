#!/usr/bin/env python3
"""Pytest test runner: one test function per bash test script.

Each function wraps one tests/test_*.sh (or scripts/test-e2e-iso.sh) and runs it
from the repo root via the run_sh fixture (tests/conftest.py). A test passes
iff its script exits 0 (lib.sh ``finish`` exits non-zero on FAIL>0;
``require_stack_up`` exits 2 when the stack is down). The script inherits this
process's fds, so output streams live (not buffered until the end).

Markers are on each test, in-file:
  integration  bash system test against the live stack (run: make start first)
  e2e          self-isolates a throwaway stack via scripts/e2e-env.sh
  long         cross-cutting tag (real-corpus drain or at-scale); stacked with
               the primary group (a test can be e2e AND long, or integration AND long)

The python unit tests are separate: native stdlib unittest.TestCase modules
(tests/test_gateway_unit.py, test_kb_check.py, test_offset_aware_chunking.py,
test_output_json.py), marked ``unit`` by tests/conftest.py. They are NOT wrapped
here; pytest collects them natively.

Select a group:  pytest -m e2e  /  -m long  /  -m "not e2e and not long".
Make targets: make test | test-unit | test-e2e | test-e2e-long.
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

@pytest.mark.integration
def test_04_openwebui_rag(run_sh):
    """Open WebUI RAG embedding endpoint + upload/embed/search. Regression guard for the persisted rag.ollama.base_url stale-host failure."""
    run_sh("tests/test_04_openwebui_rag.sh")

@pytest.mark.integration
def test_05_openwebui_user_readonly(run_sh):
    """The agent (non-admin) API key is read-scoped (temp KB, '*' read grant, write/delete deny assertions)."""
    run_sh("tests/test_05_openwebui_user_readonly.sh")

@pytest.mark.integration
def test_06_gateway(run_sh):
    """api-gateway authorization matrix (admin + agent keys; personal write, read-all, cross-user deny, admin override, spoof/claim deny, shared-group)."""
    run_sh("tests/test_06_gateway.sh")

@pytest.mark.integration
def test_07_admin_users(run_sh):
    """Admin-driven KB user provisioning via the api-gateway (POST /admin/users; create, whoami, non-admin 403, duplicate, partial-failure rollback)."""
    run_sh("tests/test_07_admin_users.sh")

@pytest.mark.e2e
@pytest.mark.long
def test_08_e2e(run_sh):
    """Isolated e2e: api-gateway Graphiti agent surface (whoami/status/groups/add/retrieve/episodes/delete-edge/delete-episode/forget) against a throwaway stack. LONG."""
    run_sh("tests/test_08_e2e.sh")

@pytest.mark.integration
@pytest.mark.long
def test_09_gdrive_index(run_sh):
    """Full real-gdrive drain via api-gateway POST /index + poll GET /status, then failure audit + deterministic semantic search. LONG."""
    run_sh("tests/test_09_gdrive_index.sh")

@pytest.mark.integration
def test_10_ocr_auth(run_sh):
    """markitdown-ocr /process auth gate (token wired end-to-end; 401 without Bearer, 200 with). Tolerant SKIP when OCR disabled."""
    run_sh("tests/test_10_ocr_auth.sh")

@pytest.mark.integration
def test_11_gdrive_index_fixture(run_sh):
    """api-gateway /index `path` parameter on a small, deterministic, committed fixture set (fast replacement for the full test_09 drain)."""
    run_sh("tests/test_11_gdrive_index_fixture.sh")

@pytest.mark.integration
def test_13_chunk_quality(run_sh):
    """Chunk-QUALITY audit over generated fixtures for all 10 allowlisted types (sliceability, span/page metadata, coalescing, offsets, fidelity)."""
    run_sh("tests/test_13_chunk_quality.sh")

@pytest.mark.integration
def test_14_rag_config(run_sh):
    """rag-config.sh re-asserts the hybrid retrieval keys over webui.db and OWUI runs the pgvector backend."""
    run_sh("tests/test_14_rag_config.sh")

@pytest.mark.integration
def test_15_retrieve(run_sh):
    """gateway-mediated POST /retrieve: Caddy route + validation matrix + all three modes 200 + lexical exact-token acceptance (pgvector FTS)."""
    run_sh("tests/test_15_retrieve.sh")

@pytest.mark.e2e
def test_12_kb_check(run_sh):
    """Isolated e2e for make kb-check: throwaway stack, upload synthetic files, create the file-{id} leak, then detect -> PURGE=1 export+purge -> re-audit 0 orphans."""
    run_sh("tests/test_12_kb_check.sh")

@pytest.mark.e2e
@pytest.mark.long
def test_e2e_iso(run_sh):
    """Isolated at-scale e2e: clone the repo to a throwaway .test-e2e/ under a separate compose project (kb-e2e) and run the destructive e2e (wipe + re-provision + rclone + full suite + test_09 drain) there. LONG."""
    run_sh("scripts/test-e2e-iso.sh")

