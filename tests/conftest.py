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

3. Provide the iso fixtures for isolated tests (throwaway clean-prod stacks,
   auto-picked free host port): ``iso_env`` (session-scoped, shared by the 10
   iso+shared tests) and ``iso_env_named`` (function-scoped factory, a named
   own-iso stack per destructive/heavy test). See their docstrings for the
   clean-env / split-setup / keep-on-teardown model.
"""

import os
import subprocess
import unittest
from pathlib import Path

import pytest

# Absolute repo root; subprocess cwd (robust to the directory pytest is run
# from). tests/ is one level below the repo root.
REPO = Path(__file__).resolve().parents[1]


def pytest_collection_modifyitems(config, items):
    """Mark every stdlib unittest.TestCase item with the ``unit`` marker.

    Uses ``item.cls`` (set for class-based items at collection time, before any
    instance is created) so this is robust at collection-modify time.
    """
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is not None and issubclass(cls, unittest.TestCase):
            item.add_marker(pytest.mark.unit)


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


def _iso_teardown(name, stamp):
    """Stop the isolated stack's containers but KEEP the clone on disk for
    inspection (the operator flushes it via ``make clean-tests`` / ``make
    clean-test NAME=<suffix> STAMP=<stamp>``). All iso runs are kept on
    teardown -- pass or fail, long or short -- so what actually ran (the
    clone's HEAD, on-disk state, logs) is inspectable after the fact. Output
    is captured: teardown is best-effort and must not noise up the pytest
    report."""
    subprocess.run(
        ["bash", "-c", '. scripts/lib-e2e-env.sh; e2e_stop_docker "%s" "%s"' % (name, stamp)],
        cwd=str(REPO), capture_output=True)


def _setup_iso_env(name, ocr, request, at_scale=False):
    """Shared isolate -> finalizer -> provision -> _run for both the named
    (function) and shared (session) iso fixtures.

    call A -- ``e2e_resolve_ollama && e2e_isolate <name> "" [<ocr>]``: progress
             -> stderr (streams live); stdout -> temp file with the captured iso
             env (whatever is set, even on isolate failure). Python parses it
             and registers a finalizer. (Inherits the operator env for
             OLLAMA_HOST resolution; e2e_isolate strips BASH_ENV/KB_HOST/
             KB_API_KEY before the clone sees them.)
    call B -- ``e2e_provision`` (clean-prod) or, when ``at_scale``,
             ``e2e_provision_at_scale`` (image rebuild + preflight + real rclone
             gdrive corpus + make ci) in the clone with the clean child env,
             output streams live. When ``at_scale``, the fixture first copies the
             source repo's deny-list (.kb-ignore chain, or the old INI
             .exclude.conf/gdrive-exclude.conf translated in-place) into the
             clone's ./root/ (the provision bash has no E2E_SRC to reach it; the
             clone-root is not wiped, so the copy survives to gdrive-sync). A failure here still
             hits the finalizer (registered after A), so no half-up stack is
             stranded.

    The body runs in a CLEAN child env (only the iso vars + PATH/HOME/LANG/TERM;
    no operator BASH_ENV, no live KB_HOST/KB_API_KEY) so the operator's profile
    never reaches the test body. Teardown ALWAYS stops the containers and keeps
    the clone on disk (``e2e_stop_docker``) -- pass or fail, long or short --
    so what ran is inspectable; flush via ``make clean-tests``. Returns a
    ``_run(relpath)`` callable."""
    setup = subprocess.run(
        ["bash", "-c", _ISO_SETUP_BASH], cwd=str(REPO),
        env={**os.environ, "E2E_NAME": name, "E2E_OCR": ocr or ""},
        stdout=subprocess.PIPE, text=True)
    captured = _parse_iso_env(setup.stdout)
    if setup.returncode != 0 or not captured.get("E2E_STAMP"):
        pytest.fail("%s isolate failed" % name, pytrace=False)
    # Register the finalizer NOW (after isolate, before provision): a provision
    # failure still stops the stack via this finalizer (the clone is kept).
    request.addfinalizer(
        lambda: _iso_teardown(captured["E2E_NAME"], captured["E2E_STAMP"]))
    if at_scale:
        # The unified deny-list (gitignored PII: Drive file paths) is read by
        # `make kb-sync` so rclone does not abort on non-downloadable paths.
        # The provision bash has no E2E_SRC (_child_env/_ISO_VARS do not carry
        # it), so the fixture -- which has REPO = the source repo root -- copies
        # it into the clone's ./root/ now, before the provision runs. The clone-root
        # is not wiped, so the copy survives.
        #
        # The deny-list is now per-directory .kb-ignore (gitignore-style). Probe the
        # live source in order: (1) ./root/.kb-ignore exists -> the live cutover ran;
        # copy every live .kb-ignore into the clone. (2) ./root/.exclude.conf (INI,
        # post-migrate but pre-cutover) -> translate to .kb-ignore in the clone.
        # (3) ./gdrive-exclude.conf (INI, pre-migration, bare [X] headers) ->
        # translate (the translator re-prefixes [X] -> gdrive/X). Fail loud if none
        # (rclone would abort anyway). Discarded with the throwaway clone (never
        # committed, never leaves the host).
        clone_root = Path(captured["E2E_CLONE"]) / "root"
        clone_root.mkdir(parents=True, exist_ok=True)
        kb_ignore_src = REPO / "root" / ".kb-ignore"
        exclude_src = REPO / "root" / ".exclude.conf"
        old_src = REPO / "gdrive-exclude.conf"
        if kb_ignore_src.is_file():
            # Live cutover already ran: copy every live .kb-ignore into the clone.
            n = 0
            for src_file in (REPO / "root").rglob(".kb-ignore"):
                rel = src_file.relative_to(REPO / "root")
                dst = clone_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")
                n += 1
            if n == 0:
                pytest.fail("root/.kb-ignore present but no .kb-ignore files found under "
                            "%s -- required for the at-scale gdrive rclone" % (REPO / "root"),
                            pytrace=False)
        elif exclude_src.is_file() or old_src.is_file():
            src = exclude_src if exclude_src.is_file() else old_src
            r = subprocess.run(
                ["python3", str(REPO / "scripts" / "exclude_to_kb_ignore.py"),
                 "--src", str(src), "--target-root", str(clone_root)],
                capture_output=True, text=True)
            if r.returncode != 0:
                pytest.fail("exclude_to_kb_ignore failed (%s): %s" % (src, r.stderr),
                            pytrace=False)
        else:
            pytest.fail("exclude deny-list missing: none of %s, %s, %s -- required "
                        "for the at-scale gdrive rclone" % (kb_ignore_src, exclude_src, old_src),
                        pytrace=False)
    prov = subprocess.run(
        ["bash", "-c", ". scripts/lib-e2e-env.sh; %s" %
            ("e2e_provision_at_scale" if at_scale else "e2e_provision")],
        cwd=captured["E2E_CLONE"], env=_child_env(captured))
    if prov.returncode != 0:
        pytest.fail("%s provision failed" % name, pytrace=False)
    child = _child_env(captured)

    def _run(relpath):
        rc = subprocess.run(["bash", relpath], cwd=captured["E2E_CLONE"],
                            env=child).returncode
        if rc != 0:
            pytest.fail("%s exited %d" % (relpath, rc), pytrace=False)
    return _run


@pytest.fixture
def iso_env_named(request):
    """Factory fixture (function scope): provision a NAMED own-iso env (a
    throwaway clean-prod stack, auto-picked free host port) for one destructive
    / heavy iso test, and return a runner that executes a repo-relative .sh
    body in it.

    The ``suffix`` names the clone leaf (operator traceability:
    ``.test-env/<stamp>-<suffix>/``). The iso env is clean / prod-exact: the only
    diffs from prod are ``KB_HOST`` (the auto-picked port) + the ephemeral
    ``KB_API_KEY`` (file-based in the clone .env.local). No env hacks. Pass
    ``at_scale=True`` for the comprehensive at-scale provision (image rebuild +
    real rclone gdrive corpus; used by test_09_gdrive_index).

    Setup is split (isolate -> finalizer -> provision) so a provision failure
    does not strand a half-up stack. Teardown ALWAYS stops the containers and
    keeps the clone on disk (``e2e_stop_docker``) -- pass or fail, long or
    short -- so what ran is inspectable afterward; flush via ``make clean-tests``
    (or ``make clean-test NAME=<suffix> STAMP=<stamp>`` for one run)."""
    def _make(suffix, ocr=None, at_scale=False):
        # All iso runs are kept on teardown (stop containers, KEEP the clone):
        # long runs (test_08/09) because they are expensive to rebuild, and now
        # short named runs (test_12/16) + the shared session stack too -- so the
        # operator can inspect what actually ran (clone HEAD, on-disk state,
        # logs) instead of relying on the pytest report alone. Flush manually
        # via `make clean-tests` (all) / `make clean-test NAME=<suffix>`. See
        # [[long-runs-never-auto-cleaned]].
        return _setup_iso_env(suffix, ocr, request, at_scale=at_scale)
    return _make


@pytest.fixture(scope="session")
def iso_env(request):
    """Session-scoped SHARED clean-prod stack for the iso+shared tests
    (test_04/05/06/07/10/11/13/14/15/17). Provisions ONE isolated stack
    (e2e_isolate "iso-shared", auto-picked port, OCR=true template default) +
    e2e_provision, shared across all of them. Each shared test's body runs in a
    CLEAN child env via the returned ``_run``; the tests self-clean (delete
    temp KBs/files) so the shared env stays clean across them. Teardown at
    session end stops the containers and KEEPS the clone on disk (pass or fail)
    so what ran is inspectable; flush via ``make clean-tests`` (or
    ``make clean-test NAME=iso-shared``)."""
    return _setup_iso_env("iso-shared", None, request)