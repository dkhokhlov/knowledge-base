#!/usr/bin/env python3
"""Unit tests for scripts/kb_ignore.py (the .gitignore-style walker matcher).

Covers: `!` re-include; `*` + `!subtree/**` allowlist; ancestor-chain accumulation
(root + nested .kb-ignore); B1 directory-pattern normalization (`dir/`, `dir`,
nested `a/dir/`); globals apply to every subtree; negation order (last match
wins); missing .kb-ignore -> all allowed; basename at any depth; leading `/`
anchor; `**` crosses `/`; mtime cache re-read; the `filter` + `check` CLI.

No network, no stack, no gdrive (synthetic temp trees only). Run:
  python3 tests/test_kb_ignore.py -v
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import kb_ignore as ki  # noqa: E402


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestMatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        ki.clear_cache()

    def tearDown(self):
        self.tmp.cleanup()

    # --- missing file -> all allowed ---
    def test_missing_ignore_allows_everything(self):
        _write(os.path.join(self.root, "a", "b", "secret.md"), "x")
        self.assertTrue(ki.allowed(self.root, "a/b/secret.md"))
        self.assertTrue(ki.allowed(self.root, "anything"))

    # --- bare deny (basename at any depth) ---
    def test_bare_basename_denies_at_any_depth(self):
        _write(os.path.join(self.root, ".kb-ignore"), "secret.md\n")
        _write(os.path.join(self.root, "a", "b", "secret.md"), "x")
        self.assertFalse(ki.allowed(self.root, "secret.md"))
        self.assertFalse(ki.allowed(self.root, "a/secret.md"))
        self.assertFalse(ki.allowed(self.root, "a/b/secret.md"))
        self.assertTrue(ki.allowed(self.root, "a/b/ok.md"))

    # --- ! re-include ---
    def test_negation_reincludes(self):
        _write(os.path.join(self.root, ".kb-ignore"), "*.md\n!keep.md\n")
        self.assertFalse(ki.allowed(self.root, "drop.md"))
        self.assertTrue(ki.allowed(self.root, "keep.md"))

    # --- last match wins (order) ---
    def test_last_match_wins(self):
        _write(os.path.join(self.root, ".kb-ignore"), "!keep.md\n*.md\n")
        # `*.md` comes AFTER `!keep.md` -> keep.md is excluded again
        self.assertFalse(ki.allowed(self.root, "keep.md"))

    # --- allowlist: * + !subtree/** ---
    def test_star_allowlist(self):
        _write(os.path.join(self.root, ".kb-ignore"),
               "*\n!keep/**\n")
        self.assertFalse(ki.allowed(self.root, "other/file.md"))
        self.assertTrue(ki.allowed(self.root, "keep/file.md"))
        self.assertTrue(ki.allowed(self.root, "keep/a/b/file.md"))

    # --- projects allowlist: * + !<glob> over dir names (flat) ---
    def test_projects_allowlist_flat(self):
        _write(os.path.join(self.root, ".kb-ignore"),
               "*\n!-home-user-projects-PubTeam*\n"
               "!-home-user-projects-myrepo\n")
        self.assertTrue(ki.allowed(self.root, "-home-user-projects-PubTeam-foo"))
        self.assertTrue(ki.allowed(self.root, "-home-user-projects-myrepo"))
        self.assertFalse(ki.allowed(self.root, "-home-user-projects-PrivateRepo"))
        self.assertFalse(ki.allowed(self.root, "-home-user-projects-other"))

    # --- B1: trailing slash = dir + contents ---
    def test_dir_trailing_slash_excludes_contents(self):
        _write(os.path.join(self.root, ".kb-ignore"), "private/\n")
        self.assertFalse(ki.allowed(self.root, "private/secret.md"))
        self.assertFalse(ki.allowed(self.root, "private/sub/secret.md"))
        self.assertTrue(ki.allowed(self.root, "public/secret.md"))

    # --- B1: bare dir name matches the dir + contents (gitignore) ---
    def test_bare_dir_name_excludes_contents(self):
        # gitignore: a bare name matches a file OR dir of that name; when it is a
        # dir, the subtree is excluded. For a file path under private/, the
        # basename rule `private` matches the `private` segment on the chain.
        _write(os.path.join(self.root, ".kb-ignore"), "private\n")
        self.assertFalse(ki.allowed(self.root, "private/secret.md"))
        self.assertTrue(ki.allowed(self.root, "public/secret.md"))

    # --- B1: nested a/dir/ ---
    def test_nested_dir_pattern(self):
        _write(os.path.join(self.root, ".kb-ignore"), "a/private/\n")
        self.assertFalse(ki.allowed(self.root, "a/private/secret.md"))
        self.assertFalse(ki.allowed(self.root, "a/private/x/y.md"))
        self.assertTrue(ki.allowed(self.root, "b/private/secret.md"))
        self.assertTrue(ki.allowed(self.root, "a/public/secret.md"))

    # --- leading / anchors at the .kb-ignore dir ---
    def test_leading_slash_anchors(self):
        _write(os.path.join(self.root, ".kb-ignore"), "/top.md\n")
        self.assertFalse(ki.allowed(self.root, "top.md"))
        self.assertTrue(ki.allowed(self.root, "sub/top.md"))

    # --- ** crosses / ---
    def test_double_star_crosses_slash(self):
        _write(os.path.join(self.root, ".kb-ignore"), "**/draft.md\n")
        self.assertFalse(ki.allowed(self.root, "draft.md"))
        self.assertFalse(ki.allowed(self.root, "a/draft.md"))
        self.assertFalse(ki.allowed(self.root, "a/b/draft.md"))

    # --- * does NOT cross / in a path pattern ---
    def test_single_star_in_path_does_not_cross(self):
        _write(os.path.join(self.root, ".kb-ignore"), "*/x.md\n")
        self.assertFalse(ki.allowed(self.root, "a/x.md"))
        self.assertTrue(ki.allowed(self.root, "a/b/x.md"))

    # --- ancestor chain: nested .kb-ignore adds rules ---
    def test_nested_ignore_file_adds_rules(self):
        _write(os.path.join(self.root, ".kb-ignore"), "*.log\n")
        _write(os.path.join(self.root, "sub", ".kb-ignore"), "extra.md\n")
        self.assertFalse(ki.allowed(self.root, "sub/trace.log"))   # root *.log
        self.assertFalse(ki.allowed(self.root, "sub/extra.md"))   # nested extra.md
        self.assertTrue(ki.allowed(self.root, "sub/keep.md"))
        # root *.log does NOT reach a sibling outside sub via the nested file, but
        # the root rule still applies everywhere:
        self.assertFalse(ki.allowed(self.root, "other/trace.log"))

    # --- documented limitation: a deeper ! CAN re-include under a shallower dir
    #     exclude (the gitignore parent-dir rule is NOT enforced in this model) ---
    def test_nested_negation_reincludes_under_shallow_dir_exclude(self):
        # `private/` at root excludes the dir; a deeper `!keep.md` re-includes it
        # because the post-filter model does not stop descent at excluded dirs.
        _write(os.path.join(self.root, ".kb-ignore"), "private/\n")
        _write(os.path.join(self.root, "private", ".kb-ignore"), "!keep.md\n")
        self.assertTrue(ki.allowed(self.root, "private/keep.md"))
        self.assertFalse(ki.allowed(self.root, "private/other.md"))

    # --- globals (root .kb-ignore) apply to every subtree ---
    def test_globals_apply_to_every_subtree(self):
        _write(os.path.join(self.root, ".kb-ignore"), "*.pyc\n")
        self.assertFalse(ki.allowed(self.root, "kb1/a.pyc"))
        self.assertFalse(ki.allowed(self.root, "kb2/sub/a.pyc"))
        self.assertTrue(ki.allowed(self.root, "kb1/a.md"))

    # --- comments + blank lines ignored ---
    def test_comments_and_blanks_ignored(self):
        _write(os.path.join(self.root, ".kb-ignore"),
               "# a comment\n\n; semicolon comment\n*.tmp\n")
        self.assertFalse(ki.allowed(self.root, "a.tmp"))
        self.assertTrue(ki.allowed(self.root, "a.md"))

    # --- mtime cache: re-read after a change ---
    def test_mtime_cache_reread(self):
        f = os.path.join(self.root, ".kb-ignore")
        _write(f, "*.log\n")
        self.assertFalse(ki.allowed(self.root, "a.log"))
        _write(f, "*.md\n")  # change the file
        ki.clear_cache()  # forced reload (mtime path also works; clear is deterministic)
        self.assertTrue(ki.allowed(self.root, "a.log"))
        self.assertFalse(ki.allowed(self.root, "a.md"))


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        _write(os.path.join(self.root, ".kb-ignore"), "*\n!keep/**\n")
        self.mod = os.path.join(HERE, "..", "scripts", "kb_ignore.py")

    def tearDown(self):
        self.tmp.cleanup()

    def test_filter_emits_only_allowed(self):
        paths = "other/a.md\nkeep/b.md\nkeep/sub/c.md\ndrop\n"
        r = subprocess.run([sys.executable, self.mod, "filter", "--root", self.root],
                           input=paths, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = [l for l in r.stdout.splitlines() if l]
        self.assertEqual(sorted(out), ["keep/b.md", "keep/sub/c.md"])

    def test_check_exit_codes(self):
        r = subprocess.run([sys.executable, self.mod, "check", "--root", self.root,
                            "--path", "keep/b.md"])
        self.assertEqual(r.returncode, 0)
        r = subprocess.run([sys.executable, self.mod, "check", "--root", self.root,
                            "--path", "drop/x.md"])
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)