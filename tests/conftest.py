"""Pytest configuration shared by every test module in tests/.

Three concerns:

1. Mark the python unit tests ``unit``. The UTs (tests/test_*.py) are stdlib
   ``unittest.TestCase`` modules kept standalone (runnable as
   ``python3 tests/test_X.py``), so they do NOT import pytest and carry no
   in-file marker. This hook marks every collected ``unittest.TestCase`` item
   ``unit`` so ``pytest -m unit`` (and ``make test-unit``) selects them. The
   bash-script wrappers (plain functions, not TestCase) are left untouched;
   they carry their own in-file ``@pytest.mark.<group>``.

2. Provide the ``run_sh`` fixture the live-stack wrappers use to run a
   repo-relative script from the repo root. The script inherits this
   process's stdout/stderr (live output, no capture) so long tests stream
   their progress; it passes iff the script exits 0 (lib.sh ``finish`` exits
   non-zero on FAIL>0; ``require_stack_up`` exits 2 when the stack is down).

3. Provide the ``e2e_env_named`` factory fixture for an OWN isolated e2e env
   (a throwaway clean-prod stack, auto-picked free host port). See its
   docstring for the clean-env / split-setup / outcome-branched teardown model.
"""

import os
import subprocess
import unittest
from pathlib import Path

import pytest

# Absolute repo root; subprocess cwd (robust to the directory pytest is run
# from). tests/ is one level below the repo root.
REPO = Path(__file__).resolve().parents[1]

# Stash key for the test's call-phase report. Set by ``pytest_runtest_makereport``
# and read by the ``e2e_env_named`` finalizer to branch teardown on whether the
# test itself passed (green -> remove the clone; fail -> keep it).
_REP = pytest.StashKey()


def pytest_collection_modifyitems(config, items):
    """Mark every stdlib unittest.TestCase item with the ``unit`` marker.

    Uses ``item.cls`` (set for class-based items at collection time, before any
    instance is created) so this is robust at collection-modify time.
    """
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is not None and issubclass(cls, unittest.TestCase):
            item.add_marker(pytest.mark.unit)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Stash the call-phase report on the item.

    A fixture finalizer (registered before the test runs, executed after it)
    reads this to branch teardown on the test's own outcome: passed -> remove
    the isolated clone (``e2e_down``); failed -> keep it for inspection
    (``e2e_stop_docker``; flush later with ``make clean-tests``).
    """
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call":
        item.stash[_REP] = rep


@pytest.fixture
def run_sh():
    """Return a callable that runs a repo-relative bash script from the repo
    root and fails the test on a non-zero exit. Output is not captured (the
    script inherits this process's fds) so progress streams live."""
    def _run(relpath):
        rc = subprocess.run(["bash", relpath], cwd=str(REPO)).returncode
        if rc != 0:
            pytest.fail("%s exited %d" % (relpath, rc), pytrace=False)
    return _run


# --- isolated e2e env: named (own) factory fixture --------------------------

# Iso env vars captured from ``e2e_isolate`` (setup) and re-injected into the
# provision + body subprocesses. The body runs with a CLEAN child env (only
# these + PATH/HOME/LANG/TERM) -- NOT the operator's shell profile -- so the
# iso env is clean / prod-exact (only KB_HOST + the ephemeral KB_API_KEY
# differ; KB_API_KEY is file-based in the clone .env.local, read by load_env,
# never carried in the process env).
_ISO_VARS = (
    "E2E_NAME", "E2E_PORT", "E2E_CLONE", "E2E_KB_HOST", "E2E_STAMP",
    "COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "OWUI_CONTAINER",
    "MARKITDOWN_CONTAINER", "POSTGRES_CONTAINER", "KB_HOST", "OLLAMA_HOST",
)

# Bash for the isolate step (call A). Progress streams to stderr (live); stdout
# holds ONLY the temp-file path with the captured KEY=VALUE iso env. The env is
# emitted whatever is set -- even on an isolate failure -- so the finalizer can
# clean a half-created stack. ``e2e_isolate`` unsets BASH_ENV/KB_HOST/KB_API_KEY
# internally, so the operator's profile does not reach the clone bootstrap.
_ISO_SETUP_BASH = r'''
_envf="$(mktemp)"
{ . scripts/lib-e2e-env.sh; e2e_resolve_ollama && e2e_isolate "$E2E_NAME" "" "$E2E_OCR"; } >&2
rc=$?
for k in E2E_NAME E2E_PORT E2E_CLONE E2E_KB_HOST E2E_STAMP \
         COMPOSE_PROJECT_NAME COMPOSE_FILE OWUI_CONTAINER MARKITDOWN_CONTAINER \
         POSTGRES_CONTAINER KB_HOST OLLAMA_HOST; do
  printf '%s=%s\n' "$k" "${!k:-}"
done > "$_envf"
printf '%s\n' "$_envf"
exit $rc
'''


def _parse_iso_env(text):
    """Read the captured iso env. ``text`` is the isolate step's stdout: its
    last line is the temp-file path; the file holds KEY=VALUE lines. Remove the
    temp file once parsed (it is only an inter-process handoff)."""
    lines = text.strip().splitlines()
    path = lines[-1] if lines else ""
    env = {}
    if path:
        try:
            with open(path) as f:
                for line in f:
                    if "=" in line:
                        k, v = line.rstrip("\n").split("=", 1)
                        env[k] = v
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    return env


def _child_env(captured):
    """Build the CLEAN child env for the provision + body subprocesses: a
    minimal shell env + the iso vars. The operator's BASH_ENV / live KB_HOST /
    live KB_API_KEY are NOT inherited (no profile pollution; the iso env is
    prod-exact). KB_API_KEY is deliberately absent -- it is file-based (clone
    .env.local), read by the body's load_env, never carried in the process env."""
    child = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", ""),
        "TERM": os.environ.get("TERM", ""),
    }
    for k in _ISO_VARS:
        if captured.get(k):
            child[k] = captured[k]
    return child


def _iso_teardown(name, stamp, remove):
    """Stop the isolated stack. ``remove`` (test passed) -> ``e2e_down`` (stop
    containers + remove the clone); else (test failed) -> ``e2e_stop_docker``
    (stop containers, KEEP the clone for inspection; flush with
    ``make clean-tests``). Output is captured: teardown is best-effort and must
    not noise up the pytest report."""
    fn = "e2e_down" if remove else "e2e_stop_docker"
    subprocess.run(
        ["bash", "-c", '. scripts/lib-e2e-env.sh; %s "%s" "%s"' % (fn, name, stamp)],
        cwd=str(REPO), capture_output=True)


def _test_passed(request):
    """Did the consuming test's call phase pass? Defaults to False (keep the
    clone) if no call report is stashed yet (e.g. an error before the call)."""
    rep = request.node.stash.get(_REP, None)
    return bool(rep and rep.passed)


@pytest.fixture
def e2e_env_named(request):
    """Factory fixture (function scope): provision a NAMED isolated e2e env (a
    throwaway clean-prod stack, auto-picked free host port) for one e2e test,
    and return a runner that executes a repo-relative .sh body in it.

    The ``suffix`` IS the clone subdir name (operator traceability:
    ``.test-<suffix>/<stamp>/``). The iso env is clean / prod-exact: the only
    diffs from prod are ``KB_HOST`` (the auto-picked port) + the ephemeral
    ``KB_API_KEY`` (file-based in the clone .env.local). No env hacks.

    Setup is split into two subprocess calls so a provision failure does not
    strand a half-up stack:

      call A -- ``e2e_resolve_ollama && e2e_isolate <suffix> "" [<ocr>]``:
               progress -> stderr (streams live); stdout -> temp file with the
               captured iso env. Python parses it and registers a finalizer.
      call B -- ``e2e_provision`` in the clone with the clean child env. A
               failure here still hits the finalizer (registered after A).

    The body runs in a CLEAN child env (only the iso vars + PATH/HOME/LANG/TERM;
    no BASH_ENV, no live KB_HOST/KB_API_KEY) so the operator's profile never
    reaches the destructive test body. Teardown branches on the test outcome:
    passed -> ``e2e_down`` (stop + remove the clone); failed ->
    ``e2e_stop_docker`` (stop, keep the clone for ``make clean-tests``).
    """
    def _make(suffix, ocr=None):
        # call A: isolate (inherits the operator env for OLLAMA_HOST resolution;
        # e2e_isolate strips BASH_ENV/KB_HOST/KB_API_KEY before the clone sees them).
        setup = subprocess.run(
            ["bash", "-c", _ISO_SETUP_BASH], cwd=str(REPO),
            env={**os.environ, "E2E_NAME": suffix, "E2E_OCR": ocr or ""},
            stdout=subprocess.PIPE, text=True)
        captured = _parse_iso_env(setup.stdout)
        if setup.returncode != 0 or not captured.get("E2E_STAMP"):
            pytest.fail("e2e_env_named(%s) isolate failed" % suffix, pytrace=False)
        # Register the finalizer NOW (after isolate, before provision): a
        # provision failure still tears the stack down via this finalizer.
        request.addfinalizer(
            lambda: _iso_teardown(captured["E2E_NAME"], captured["E2E_STAMP"],
                                  remove=_test_passed(request)))
        # call B: provision in the clone, clean child env, output streams live.
        prov = subprocess.run(
            ["bash", "-c", ". scripts/lib-e2e-env.sh; e2e_provision"],
            cwd=captured["E2E_CLONE"], env=_child_env(captured))
        if prov.returncode != 0:
            pytest.fail("e2e_env_named(%s) provision failed" % suffix, pytrace=False)
        child = _child_env(captured)

        def _run(relpath):
            rc = subprocess.run(["bash", relpath], cwd=captured["E2E_CLONE"],
                                env=child).returncode
            if rc != 0:
                pytest.fail("%s exited %d" % (relpath, rc), pytrace=False)
        return _run
    return _make