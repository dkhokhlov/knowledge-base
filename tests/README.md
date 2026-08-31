# Tests

A regular pytest suite. The bash test scripts are wrapped by one runner,
`tests/test_runner.py`, with **one test function per `.sh`** — each function
carries its own in-file marker + docstring. The python unit tests are separate
native modules. One make target per group selects by marker.

## Tiers + markers

Tests are classified by **env need**, with one criterion: *a test must not
contaminate the env for subsequent tests; if its effects are hard to recover, it
gets its own isolated (iso) env*. The iso env is **clean = exactly the prod env**;
the only diffs are `KB_HOST` (an auto-picked port) + the ephemeral `KB_API_KEY`
(file-based in the clone `.env.local`). No env hacks or workarounds.

`shared` and `long` are **cross-cutting tags**: a test carries them ALONGSIDE its
primary group (a test can be `iso` AND `shared`, or `iso` AND `long`). The primary
groups are `unit`, `integration`, `iso`.

| Marker | Meaning | Needs |
|---|---|---|
| `unit` | python unit test (stdlib `unittest`, no stack) | nothing |
| `integration` | bash system test against the LIVE stack, read-only | `make start` |
| `iso` | isolated test against a throwaway clean-prod stack (`scripts/lib-e2e-env.sh`) | nothing (self-isolates; GPU/RAM: a 2nd stack) |
| `shared` | cross-cutting tag: the `iso` tests that share ONE session-provisioned stack | — |
| `long` | cross-cutting tag: long-running (real-corpus drain, at-scale) | patient run |

| Tier | Tests | Markers | Why |
|---|---|---|---|
| no stack | `test_gateway_unit`, `test_kb_check`, `test_offset_aware_chunking`, `test_output_json` | `unit` | stdlib UTs |
| live (RO) | `test_01`, `test_02`, `test_03` | `integration` | read-only; verify the LIVE deployment's infra (Neo4j not host-published, pg_isready on live kb-postgres) |
| shared-iso | `test_04`, `05`, `06`, `07`, `10`, `11`, `13`, `14`, `15` | `iso` + `shared` | contaminate but self-clean (traps delete temp KBs/files) → share ONE clean-prod stack |
| own-iso (named) | `test_08_e2e`, `test_09_gdrive_index`, `test_12_kb_check`, `test_e2e_iso` | `iso` (+`long`) | destructive / heavy / no self-cleanup → each gets its own named clean-prod stack |

`test_09` is currently `integration long` (runs in `make test-e2e-iso`'s clone + as
`make test-long`); it migrates to own-iso (`iso long`) in a follow-up.

## Make targets

Leaf targets select by marker; aggregator targets compose the leaves.

| Target | Selector / deps | Runs |
|---|---|---|
| `make test` | `test-unit` + `test-live-RO` | `unit` + live-RO (`test_01/02/03`) |
| `make test-unit` | `-m unit` | the python UTs |
| `make test-live-RO` | `-m "integration and not long"` | live-RO (`test_01/02/03`) |
| `make test-iso` | `test-iso-shared` + `test-iso-single` | all short iso (shared + single) |
| `make test-iso-shared` | `-m "iso and shared"` | the 9 shared-iso tests (one session stack) |
| `make test-iso-single` | `-m "iso and not long and not shared"` | the named own-iso short tests (`test_12`) |
| `make test-long` | `test-iso-long` | long iso (`test_08` + `test_e2e_iso`) |
| `make test-iso-long` | `-m "iso and long"` | long iso (`test_08` + `test_e2e_iso`) |
| `make test-e2e-iso` | `scripts/test-e2e-iso.sh` | at-scale from-scratch (clean-all + re-provision + rclone + `test_09` drain) |
| `make test-output` | `python3 tests/test_output_json.py -v` | standalone JSON-schema UT (no stack) |

`make test` needs the live stack up (`make start`). The iso targets self-isolate
(never touch the live stack): the first `iso` test auto-provisions a clean-prod
stack via the `iso_env` / `iso_env_named` fixture (`tests/conftest.py`). Run a raw
selection with `python3 -m pytest -m <expr>`.

## Ports

The iso fixtures **auto-pick a free host port** in `3011..3099`, skipping `3000`
(live) and `3010` (`make test-e2e-iso`'s canonical port). No manual port
assignment. `make test-e2e-iso` keeps `3010` (`E2E_PORT` override).

## Layout

Two kinds of test, both collected natively by pytest:

- **Bash-script wrappers** — `tests/test_runner.py` is ONE runner module with
  one test function per `.sh`. Each function is decorated with its marker
  (`@pytest.mark.integration` / `@pytest.mark.iso`, with `@pytest.mark.shared` /
  `@pytest.mark.long` stacked as cross-cutting tags), has a docstring, and calls a
  fixture (`tests/conftest.py`) with the `.sh` path:
    - `run_sh` — live stack (`integration`, `test_01/02/03`). Runs `bash <script>`
      from the repo root with the caller's env; output streams live.
    - `iso_env` — session-shared clean-prod stack (`iso`+`shared`, 9 tests). The
      first test provisions ONE stack; each body runs in a CLEAN child env (only
      the iso vars + `PATH`/`HOME`/`LANG`/`TERM`; no operator `BASH_ENV`, no live
      `KB_HOST`/`KB_API_KEY`) so the operator profile never reaches the body.
    - `iso_env_named` — a named own-iso stack (`iso`+`long` / `iso` single). A
      function-scoped factory: `_make(suffix)` provisions a stack whose clone
      subdir is `.test-<suffix>/<stamp>/` (operator traceability).
  The test passes iff the script exits 0. Output is not captured, so progress
  streams live. Teardown: test passed → `e2e_down` (remove the clone); failed →
  `e2e_stop_docker` (keep the clone for inspection; flush with `make clean-tests`).
- **Python unit tests** — `tests/test_gateway_unit.py`, `test_kb_check.py`,
  `test_offset_aware_chunking.py`, `test_output_json.py`. Stdlib
  `unittest.TestCase`; standalone-runnable (`python3 tests/test_X.py -v`).
  They do NOT import pytest. `tests/conftest.py` auto-marks their items `unit`
  (`pytest_collection_modifyitems`), so they stay standalone and still appear
  under `pytest -m unit`.

`.sh` files are not python, so pytest never collects them directly — only the
runner's functions are. `tests/lib.sh` is the shared bash helpers (`pass`/`fail`/
`section`/`finish`/`load_env`/`require_env`/`require_stack_up`/`kb_host`); not a
test. `scripts/lib-e2e-env.sh` is the reusable iso isolation lib; not a test.
`tests/fixtures_chunkq_gen.py` is the deterministic generator that produced the
committed chunk-quality fixtures at `gdrive/.tests/chunkq/` (not a test itself;
regenerate with `--out gdrive/.tests/chunkq`). test_13 runs it with
`--manifest-only` to re-derive the manifest JSON oracle without writing files.

## Inventory

| Test function (in `test_runner.py`) | Script | Markers |
|---|---|---|
| `test_01_health` | `tests/test_01_health.sh` | `integration` |
| `test_02_gateway` | `tests/test_02_gateway.sh` | `integration` |
| `test_03_openwebui_rest` | `tests/test_03_openwebui_rest.sh` | `integration` |
| `test_04_openwebui_rag` | `tests/test_04_openwebui_rag.sh` | `iso shared` |
| `test_05_openwebui_user_readonly` | `tests/test_05_openwebui_user_readonly.sh` | `iso shared` |
| `test_06_gateway` | `tests/test_06_gateway.sh` | `iso shared` |
| `test_07_admin_users` | `tests/test_07_admin_users.sh` | `iso shared` |
| `test_08_e2e` | `tests/test_08_e2e.sh` | `iso long` |
| `test_09_gdrive_index` | `tests/test_09_gdrive_index.sh` | `integration long` |
| `test_10_ocr_auth` | `tests/test_10_ocr_auth.sh` | `iso shared` |
| `test_11_gdrive_index_fixture` | `tests/test_11_gdrive_index_fixture.sh` | `iso shared` |
| `test_13_chunk_quality` | `tests/test_13_chunk_quality.sh` | `iso shared` |
| `test_14_rag_config` | `tests/test_14_rag_config.sh` | `iso shared` |
| `test_15_retrieve` | `tests/test_15_retrieve.sh` | `iso shared` |
| `test_12_kb_check` | `tests/test_12_kb_check.sh` | `iso` |
| `test_e2e_iso` | `scripts/test-e2e-iso.sh` | `iso long` |
| (native UT) | `test_gateway_unit.py` | `unit` |
| (native UT) | `test_kb_check.py` | `unit` |
| (native UT) | `test_offset_aware_chunking.py` | `unit` |
| (native UT) | `test_output_json.py` | `unit` |

## How to add a new test

A test passes iff it exits 0. Pick the tier by the contamination criterion, add
the marker on the test in-file.

1. Decide the tier + marker:
   - **no stack** → `unit`. Name it `tests/test_<name>.py`, use
     `unittest.TestCase`, end with `if __name__ == "__main__": unittest.main()`.
     Do NOT import pytest; `conftest.py` auto-marks `unittest.TestCase` items
     `unit`. Run standalone: `python3 tests/test_<name>.py -v`.
   - **live read-only** (no contamination) → `integration`. Write the script
     `tests/test_<name>.sh` (source `tests/lib.sh`, call `load_env` +
     `require_stack_up`, use `pass`/`fail`/`finish`), then add a function to
     `tests/test_runner.py`:
     ```python
     @pytest.mark.integration
     def test_<name>(run_sh):
         """<one-line description of what the script exercises>"""
         run_sh("tests/test_<name>.sh")
     ```
   - **contaminates but self-cleans** (no cross-test contamination) → `iso` +
     `shared`. Write the body `tests/test_<name>.sh` (source `tests/lib.sh`,
     `load_env` + `require_env` for the KB key / container vars; self-clean via a
     trap that deletes temp KBs/files). Wire it to the session `iso_env` fixture:
     ```python
     @pytest.mark.iso
     @pytest.mark.shared
     def test_<name>(iso_env):
         """<one-line description>"""
         iso_env("tests/test_<name>.sh")
     ```
     It shares ONE session-provisioned clean-prod stack with the other shared
     tests, so it MUST self-clean.
   - **destructive / heavy / no self-cleanup** → `iso` (own-iso, named). The body
     is BODY-ONLY: the `iso_env_named` fixture owns `e2e_isolate` +
     `e2e_provision` + teardown. Write `tests/test_<name>.sh` (source `lib.sh`,
     `load_env` + `require_env`; NO isolate/provision/down — the fixture does
     that). Wire it to the factory:
     ```python
     @pytest.mark.iso
     def test_<name>(iso_env_named):
         """<one-line description>"""
         iso_env_named("<suffix>")("tests/test_<name>.sh")
     ```
     The `<suffix>` is the clone subdir name (operator traceability:
     `.test-<suffix>/<stamp>/`). See `test_08_e2e.sh` / `test_12_kb_check.sh`.
   - **long-running** (minutes: real-corpus drain, at-scale) → ALSO stack
     `@pytest.mark.long` on the runner function.
2. No port to pick: the iso fixtures auto-pick a free port in `3011..3099`
   (skip `3000` live + `3010` `test-e2e-iso`).
3. Run it: `python3 -m pytest -m <marker>` or the matching make target.

## Module names (no `owui` clash)

The skill wrapper is one self-contained module, `skills/claude/scripts/kb.py`
(was two: `owui.py` + `kb_gateway.py`). The gateway's `gateway/owui.py` is a
separate file copied into the gateway image. With the skill module renamed
`owui` → `kb`, the two no longer share a module name, so no
`sys.modules["owui"]` eviction is needed and the test files no longer do the
`sys.modules.pop("owui", None)` dance. `gateway/owui.py` binds unambiguously.

## Prerequisites

`make` and `uv` (install: <https://docs.astral.sh/uv/>). `make ci` provisions the
Python 3.12 `.venv` — `uv sync` from `pyproject.toml` + `uv.lock` (installs
pytest). It is a prereq of every test target, so `make test`, `make test-unit`,
etc. run it automatically (idempotent). Provision it manually with `make ci`.
The `.venv/` is gitignored; `uv.lock` is committed (the pinned resolution).

No per-test timeout — long tests carry their own internal deadlines; pick the
group via markers. The subprocess inherits the parent fds, so output streams
live (not buffered until the end).