#!/usr/bin/env python3
"""Unit tests for kb._select_projects (the projects-walker selection helper).

Selects project dirs under a root with a `memory/` subdir, filtered by `--project`
(substring) FIRST, then by <root>/.kb-ignore (with `!`). `--project` does NOT
override .kb-ignore. Pure: no stack, no network, no gdrive (synthetic temp trees).
This covers the .kb-ignore matcher CLONED into kb.py (the canonical matcher UTs
live in tests/test_kb_ignore.py). Run:
  python3 tests/test_kb_projects_select.py -v
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "claude", "scripts"))
import kb  # noqa: E402


def _mkproj(root, encoded, with_memory=True):
    d = os.path.join(root, encoded)
    if with_memory:
        os.makedirs(os.path.join(d, "memory"))
    else:
        os.makedirs(d)
    return d


def _names(pairs):
    return sorted(e for e, _ in pairs)


class TestSelectProjects(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        kb._ki_clear_cache()

    def tearDown(self):
        self.tmp.cleanup()

    def _ignore(self, text):
        with open(os.path.join(self.root, ".kb-ignore"), "w", encoding="utf-8") as fh:
            fh.write(text)

    # --- absent .kb-ignore -> all candidates (_select_projects is pure: no
    #     warning; the full-scan warning lives in cmd_index_projects) ---
    def test_absent_ignore_selects_all(self):
        _mkproj(self.root, "alpha")
        _mkproj(self.root, "beta")
        _mkproj(self.root, "gamma", with_memory=False)  # no memory/ -> skipped
        err = io.StringIO()
        with redirect_stderr(err):
            sel = kb._select_projects(self.root, None)
        self.assertEqual(_names(sel), ["alpha", "beta"])
        self.assertEqual(err.getvalue(), "")  # pure helper: no stderr

    # --- absent .kb-ignore + --project -> scoped candidates, no warning ---
    def test_absent_ignore_with_project(self):
        _mkproj(self.root, "alpha")
        err = io.StringIO()
        with redirect_stderr(err):
            sel = kb._select_projects(self.root, "alpha")
        self.assertEqual(_names(sel), ["alpha"])
        self.assertEqual(err.getvalue(), "")

    # --- * + !a* allowlist ---
    def test_star_allowlist(self):
        self._ignore("*\n!a*\n")
        _mkproj(self.root, "alpha")
        _mkproj(self.root, "apple")
        _mkproj(self.root, "beta")
        _mkproj(self.root, "gamma")
        sel = kb._select_projects(self.root, None)
        self.assertEqual(_names(sel), ["alpha", "apple"])

    # --- --project does NOT override .kb-ignore ---
    def test_project_does_not_override_ignore(self):
        # `--project beta` matches 'beta' as a substring, but .kb-ignore excludes it.
        self._ignore("*\n!alpha\n")
        _mkproj(self.root, "alpha")
        _mkproj(self.root, "beta")
        sel = kb._select_projects(self.root, "beta")
        self.assertEqual(_names(sel), [])  # beta excluded by .kb-ignore despite --project

    def test_project_substring_then_ignore_narrows(self):
        # B6: --project 'home' matches both 'home-a' and 'home-b'; .kb-ignore
        # allows only 'home-a' -> only home-a selected.
        self._ignore("*\n!home-a\n")
        _mkproj(self.root, "home-a")
        _mkproj(self.root, "home-b")
        sel = kb._select_projects(self.root, "home")
        self.assertEqual(_names(sel), ["home-a"])

    # --- the allowlist glob shape (* + !prefix* + !exact; --project narrows) ---
    def test_allowlist_glob_shape(self):
        self._ignore("*\n!-home-user-projects-PubTeam*\n"
                     "!-home-user-projects-myrepo\n")
        _mkproj(self.root, "-home-user-projects-PubTeam-foo")
        _mkproj(self.root, "-home-user-projects-myrepo")
        _mkproj(self.root, "-home-user-projects-other")
        _mkproj(self.root, "-home-user-projects-PrivateRepo")
        # full scan -> only the two allowlisted
        self.assertEqual(_names(kb._select_projects(self.root, None)),
                         ["-home-user-projects-PubTeam-foo",
                          "-home-user-projects-myrepo"])
        # --project 'projects' matches all four (substring); .kb-ignore keeps only
        # the two allowlisted.
        self.assertEqual(_names(kb._select_projects(self.root, "projects")),
                         ["-home-user-projects-PubTeam-foo",
                          "-home-user-projects-myrepo"])
        # --project 'myrepo' matches myrepo only (substring); .kb-ignore keeps it.
        self.assertEqual(_names(kb._select_projects(self.root, "myrepo")),
                         ["-home-user-projects-myrepo"])

    # --- '-' and '' encoded dirs skipped ---
    def test_dash_and_empty_skipped(self):
        self._ignore("!*\n")  # allow everything
        _mkproj(self.root, "-")  # '-' with a memory dir -> skipped by the guard
        _mkproj(self.root, "real")
        sel = kb._select_projects(self.root, None)
        self.assertEqual(_names(sel), ["real"])

    # --- --project substring filter applies when no .kb-ignore ---
    def test_project_substring_filter(self):
        _mkproj(self.root, "alpha")
        _mkproj(self.root, "alpine")
        _mkproj(self.root, "beta")
        self.assertEqual(_names(kb._select_projects(self.root, "al")),
                         ["alpha", "alpine"])

    # --- projects without memory/ are skipped even if allowlisted ---
    def test_no_memory_skipped(self):
        self._ignore("!*\n")
        _mkproj(self.root, "alpha", with_memory=False)
        _mkproj(self.root, "beta")
        self.assertEqual(_names(kb._select_projects(self.root, None)), ["beta"])


if __name__ == "__main__":
    unittest.main(verbosity=2)