#!/usr/bin/env python3
"""Unit test for the gdrive-sync two-pass filter + deletion reconcile pipelines
(Phase 4b). No stack: builds a synthetic ./root tree with per-directory .kb-ignore
files + a fake remote listing, then runs the EXACT bash pipelines gdrive-sync uses
(awk prefix | scripts/kb_ignore.py filter --root root | awk strip; the deletion
reconcile: find -printf | set-diff | filter | mv to backup) and asserts:

  * the allowed list = remote files NOT denied by the .kb-ignore ancestor chain;
  * the deletion reconcile backs up local files removed from Drive (allowed ones),
    and KEEPS protected files (dot-names; .sync-reports; denied by .kb-ignore).

The filter + denied-check pipelines are run via subprocess (the real awk +
kb_ignore.py CLI), so an awk off-by-one or a matcher regression is caught here.
The set-diff (comm) is computed in python (comm is trivial + standard)."""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(ROOT, ".."))
KB_IGNORE = os.path.join(REPO, "scripts", "kb_ignore.py")


def _run(cmd, input_text=""):
    """Run cmd (list) with stdin=input_text; return stdout. Fails on non-zero."""
    r = subprocess.run(cmd, input=input_text, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("cmd %r failed (%d): %s" % (cmd, r.returncode, r.stderr))
    return r.stdout


class GdriveSyncTwoPassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "root")          # ./root
        self.target = os.path.join(self.root, "gdrive", "MyDrive")  # ./root/gdrive/MyDrive
        self.backup_root = os.path.join(self.tmp, "backup")
        os.makedirs(self.target)
        os.makedirs(self.backup_root)
        self.safe = "MyDrive"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _ignore(self, rel, text):
        """Write a .kb-ignore at <root>/<rel>/.kb-ignore (rel='' = root)."""
        d = os.path.join(self.root, rel) if rel else self.root
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".kb-ignore"), "w") as f:
            f.write(text)

    def _touch(self, relpath_under_target, content="x"):
        """Create a local file at target/<relpath>."""
        p = os.path.join(self.target, relpath_under_target)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)

    def _filter(self, listing):
        """Exact gdrive-sync main filter pipeline:
        awk prefix gdrive/<safe>/ | kb_ignore.py filter --root root | awk strip."""
        p = "gdrive/%s/" % self.safe
        out = _run(["awk", "-v", "p=" + p, "{print p $0}"], listing)
        out = _run(["python3", KB_IGNORE, "filter", "--root", self.root], out)
        out = _run(["awk", "-v", "p=" + p,
                    "index($0,p)==1{print substr($0,length(p)+1)}"], out)
        return out

    def _denied_check(self, local_only):
        """Exact gdrive-sync reconcile denied-check: prefix | filter | strip.
        Returns the allowed (not-protected) subset of local_only."""
        return self._filter(local_only)

    def _local_listing(self):
        """Exact gdrive-sync reconcile local file list:
        find target -type f -not -path '*/.sync-reports/*' -not -name '.*' -printf %P."""
        out = _run(["find", self.target, "-type", "f",
                    "-not", "-path", "*/.sync-reports/*",
                    "-not", "-name", ".*", "-printf", "%P\\n"])
        return out

    @staticmethod
    def _lines(text):
        return sorted(l for l in text.splitlines() if l)

    def test_filter_drops_denied_keeps_allowed(self):
        # globals (*.py) + per-drive (secret.docx) + nested; verify the ancestor
        # chain: keep.docx + notes/keep.md allowed; secret.docx + notes/script.py
        # denied.
        self._ignore("", "*.py\n")
        self._ignore("gdrive/MyDrive", "secret.docx\n")
        remote = "keep.docx\nnotes/keep.md\nnotes/script.py\nsecret.docx\n"
        allowed = self._filter(remote)
        self.assertEqual(self._lines(allowed), ["keep.docx", "notes/keep.md"])

    def test_filter_no_kb_ignore_is_noop(self):
        # No .kb-ignore -> every remote file allowed.
        remote = "a.docx\nsub/b.md\n"
        self.assertEqual(self._lines(self._filter(remote)), ["a.docx", "sub/b.md"])

    def test_filter_negation_reinclude(self):
        # *.pdf then !keep.pdf -> only keep.pdf survives.
        self._ignore("gdrive/MyDrive", "*.pdf\n!keep.pdf\n")
        remote = "drop.pdf\nkeep.pdf\nsub/drop2.pdf\n"
        self.assertEqual(self._lines(self._filter(remote)), ["keep.pdf"])

    def test_reconcile_backs_up_removed_allows_keeps_protected(self):
        # remote: keep.docx (allowed), secret.docx (denied by gdrive/MyDrive/.kb-ignore),
        #         notes/keep.md (allowed). old.docx + old.py are LOCAL-ONLY (removed
        #         from Drive). .meta + .kb-ignore are dot-names (protected, dropped
        #         by find -not -name '.*').
        self._ignore("", "*.py\n")                 # global: deny .py
        self._ignore("gdrive/MyDrive", "secret.docx\n")
        remote = "keep.docx\nnotes/keep.md\nsecret.docx\n"
        remote_set = set(remote.splitlines())
        # local tree
        self._touch("keep.docx")            # in remote -> keep
        self._touch("secret.docx")          # in remote (denied) -> keep
        self._touch("old.docx")             # NOT in remote, allowed -> BACK UP
        self._touch("old.py")               # NOT in remote, denied by *.py -> KEEP
        self._touch(".meta")                # dot-name -> protected (dropped by find)
        self._touch("notes/keep.md")        # in remote -> keep

        # --- replicate the reconcile ---
        local_listing = self._local_listing()
        self.assertEqual(self._lines(local_listing),
                         ["keep.docx", "notes/keep.md", "old.docx", "old.py", "secret.docx"])
        local_set = set(local_listing.splitlines()) - {""}
        local_only = sorted(local_set - remote_set)   # comm -23 local remote
        self.assertEqual(local_only, ["old.docx", "old.py"])
        # denied-check: only old.docx is allowed (old.py denied by *.py -> protected)
        allowed_local_only = self._denied_check("\n".join(local_only) + "\n")
        self.assertEqual(self._lines(allowed_local_only), ["old.docx"])

        # perform the backup moves (the non-dry_run branch)
        for rel in self._lines(allowed_local_only):
            dst = os.path.join(self.backup_root, self.safe, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(os.path.join(self.target, rel), dst)

        # old.docx moved to backup; old.py + .meta + .kb-ignore kept; remote files kept
        remaining = self._lines(_run(["find", self.target, "-type", "f", "-printf", "%P\\n"]))
        self.assertEqual(remaining,
                         [".kb-ignore", ".meta", "keep.docx", "notes/keep.md", "old.py", "secret.docx"])
        backed_up = self._lines(_run(["find", self.backup_root, "-type", "f", "-printf", "%P\\n"]))
        self.assertEqual(backed_up, ["MyDrive/old.docx"])

    def test_reconcile_empty_local_only_no_move(self):
        # local == remote -> local_only empty -> no backup, nothing moved.
        self._touch("keep.docx")
        remote = "keep.docx\n"
        local_set = set(self._local_listing().splitlines()) - {""}
        local_only = sorted(local_set - set(remote.splitlines()))
        self.assertEqual(local_only, [])
        backed_up = self._lines(_run(["find", self.backup_root, "-type", "f", "-printf", "%P\\n"]))
        self.assertEqual(backed_up, [])


if __name__ == "__main__":
    unittest.main()