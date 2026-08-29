# Tests

A regular pytest suite. The bash test scripts are wrapped by one runner,
`tests/test_runner.py`, with **one test function per `.sh`** — each function
carries its own in-file marker + docstring. The python unit tests are separate
native modules. One make target per group selects by marker.

## Markers

Four markers. `long` is a **cross-cutting tag**: a test carries it ALONGSIDE its
primary group (a test can be `e2e` AND `long`, or `integration` AND `long`). The
other three are primary groups (a test belongs to one).

| Marker | Meaning | Needs |
|---|---|---|
| `unit` | python unit test (stdlib `unittest`, no stack) | nothing |
| `integration` | bash system test against the live stack | `make start` |
| `e2e` | isolated e2e: self-clones a throwaway stack via `scripts/e2e-env.sh` | nothing (self-isolates; GPU/RAM: a 2nd stack) |
| `long` | long-running (real-corpus drain, at-scale); cross-cutting | patient run |

## Make targets

| Target | Selector | Runs |
|---|---|---|
| `make test` | `-m "not e2e and not long"` | `unit` + `integration` (fast set) |
| `make test-unit` | `-m unit` | the python UTs |
| `make test-e2e` | `-m "e2e and not long"` | quick isolated e2e (`test_12`) |
| `make test-e2e-long` | `-m "e2e and long"` | long isolated e2e (`test_08` + `test_e2e_iso`) |
| `make test-e2e-iso` | `scripts/test-e2e-iso.sh` | at-scale from-scratch (backcompat) |
| `make test-output` | `python3 tests/test_output_json.py -v` | standalone JSON-schema UT (no stack) |

`make test` needs the live stack up (`make start`); the e2e targets self-isolate
(never touch the live stack). Run a raw selection with `python3 -m pytest -m <expr>`.

## Layout

Two kinds of test, both collected natively by pytest:

- **Bash-script wrappers** — `tests/test_runner.py` is ONE runner module with
  one test function per `.sh`. Each function is decorated with its marker
  (`@pytest.mark.integration` / `@pytest.mark.e2e`, with `@pytest.mark.long`
  stacked for long tests), has a docstring, and calls the `run_sh` fixture
  (from `tests/conftest.py`) with the `.sh` path. `run_sh` runs `bash <script>`
  from the repo root with the caller's env; the test passes iff the script
  exits 0. Output is not captured, so progress streams live.
- **Python unit tests** — `tests/test_gateway_unit.py`, `test_kb_check.py`,
  `test_offset_aware_chunking.py`, `test_output_json.py`. Stdlib
  `unittest.TestCase`; standalone-runnable (`python3 tests/test_X.py -v`).
  They do NOT import pytest. `tests/conftest.py` auto-marks their items `unit`
  (`pytest_collection_modifyitems`), so they stay standalone and still appear
  under `pytest -m unit`.

`.sh` files are not python, so pytest never collects them directly — only the
runner's functions are. `tests/lib.sh` is the shared bash helpers (`pass`/`fail`/
`section`/`finish`/`load_env`/`require_env`/`require_stack_up`/`kb_host`); not a
test. `scripts/e2e-env.sh` is the reusable e2e isolation lib; not a test.

## Inventory

| Test function (in `test_runner.py`) | Script | Markers |
|---|---|---|
| `test_01_health` | `tests/test_01_health.sh` | `integration` |
| `test_02_gateway` | `tests/test_02_gateway.sh` | `integration` |
| `test_03_openwebui_rest` | `tests/test_03_openwebui_rest.sh` | `integration` |
| `test_04_openwebui_rag` | `tests/test_04_openwebui_rag.sh` | `integration` |
| `test_05_openwebui_user_readonly` | `tests/test_05_openwebui_user_readonly.sh` | `integration` |
| `test_06_gateway` | `tests/test_06_gateway.sh` | `integration` |
| `test_07_admin_users` | `tests/test_07_admin_users.sh` | `integration` |
| `test_08_e2e` | `tests/test_08_e2e.sh` | `e2e long` |
| `test_09_gdrive_index` | `tests/test_09_gdrive_index.sh` | `integration long` |
| `test_10_ocr_auth` | `tests/test_10_ocr_auth.sh` | `integration` |
| `test_11_gdrive_index_fixture` | `tests/test_11_gdrive_index_fixture.sh` | `integration` |
| `test_12_kb_check` | `tests/test_12_kb_check.sh` | `e2e` |
| `test_e2e_iso` | `scripts/test-e2e-iso.sh` | `e2e long` |
| (native UT) | `test_gateway_unit.py` | `unit` |
| (native UT) | `test_kb_check.py` | `unit` |
| (native UT) | `test_offset_aware_chunking.py` | `unit` |
| (native UT) | `test_output_json.py` | `unit` |

## How to add a new test

A test passes iff it exits 0. Add the marker on the test, in-file.

1. Decide the kind + marker:
   - python unit test, no stack → `unit`. Name it `tests/test_<name>.py`,
     use `unittest.TestCase`, end with `if __name__ == "__main__": unittest.main()`.
     Do NOT import pytest; `conftest.py` auto-marks `unittest.TestCase` items
     `unit`. Run standalone: `python3 tests/test_<name>.py -v`.
   - bash system test against the live stack → `integration`. Write the script
     `tests/test_<name>.sh` (source `tests/lib.sh`, call `load_env` +
     `require_stack_up`, use `pass`/`fail`/`finish`), then add a function to
     `tests/test_runner.py`:
     ```python
     @pytest.mark.integration
     def test_<name>(run_sh):
         """<one-line description of what the script exercises>"""
         run_sh("tests/test_<name>.sh")
     ```
   - isolated e2e (destructive, must not touch the live stack) → `e2e`. Source
     `scripts/e2e-env.sh`, call `e2e_resolve_ollama` → `e2e_isolate <NAME> <PORT>`
     → `e2e_provision` → body → `e2e_down` in an EXIT trap. See `test_08_e2e.sh`
     / `test_12_kb_check.sh` for the pattern (ISOLATED flag + `*_KEEP`). Then add
     the runner function with `@pytest.mark.e2e`.
   - long-running (minutes: real-corpus drain, at-scale) → ALSO stack
     `@pytest.mark.long` on the runner function.
2. Pick a port for an isolated e2e that does not collide: live `3000`, e2e
   `3010`, kbcheck `3020`, test08 `3030`. Add yours to the next free port.
3. Run it: `python3 -m pytest -m <marker>` or the matching make target.

## The `owui` name clash (why two UTs evict `sys.modules["owui"]`)

Two modules are both named `owui`: `gateway/owui.py` (has `upload_file`) and
`skills/claude/scripts/owui.py` (does not). `gateway/app.py` does `import owui`
(transitive), and `test_output_json.py` does `import owui` (direct). Native
pytest collection imports every test module in ONE process, so
`sys.modules["owui"]` can hold only one. `test_gateway_unit.py` and
`test_output_json.py` each `sys.modules.pop("owui", None)` before their imports,
so each chain re-resolves to its own `owui`. The other chain's already-bound
references are unaffected — a module global binds once, at import time, and is
not re-resolved. (Renaming a source `owui` would also fix it but touches the
deployed skill; the eviction is surgical and stays in the test files.)

## Prerequisites

`make` and `uv` (install: <https://docs.astral.sh/uv/>). `make ci` provisions the
Python 3.12 `.venv` — `uv sync` from `pyproject.toml` + `uv.lock` (installs
pytest). It is a prereq of every test target, so `make test`, `make test-unit`,
etc. run it automatically (idempotent). Provision it manually with `make ci`.
The `.venv/` is gitignored; `uv.lock` is committed (the pinned resolution).

No per-test timeout — long tests carry their own internal deadlines; pick the
group via markers. The subprocess inherits the parent fds, so output streams
live (not buffered until the end).