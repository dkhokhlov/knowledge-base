"""Pytest configuration shared by every test module in tests/.

Two concerns:

1. Mark the python unit tests `unit`. The UTs (tests/test_*.py) are stdlib
   ``unittest.TestCase`` modules kept standalone (runnable as
   ``python3 tests/test_X.py``), so they do NOT import pytest and carry no
   in-file marker. This hook marks every collected ``unittest.TestCase`` item
   ``unit`` so ``pytest -m unit`` (and ``make test-unit``) selects them. The
   bash-script wrappers (plain functions, not TestCase) are left untouched;
   they carry their own in-file ``@pytest.mark.<group>``.

2. Provide the ``run_sh`` fixture the bash wrappers use to run a repo-relative
   script from the repo root. The script inherits this process's stdout/stderr
   (live output, no capture) so long tests stream their progress; it passes iff
   the script exits 0 (lib.sh ``finish`` exits non-zero on FAIL>0;
   ``require_stack_up`` exits 2 when the stack is down).
"""

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