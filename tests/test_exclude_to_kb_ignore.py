#!/usr/bin/env python3
"""Unit tests for scripts/exclude_to_kb_ignore.py (Phase 4d): the INI .exclude.conf
-> per-directory .kb-ignore translator. Verifies section->location mapping, the
bare-[X] re-prefix, comment/blank dropping, and that the translated .kb-ignore
chain reproduces the INI's deny behavior (parity via scripts/kb_ignore.py).

Drive names below are SYNTHETIC placeholders, not any real corpus."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(ROOT, ".."))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import exclude_to_kb_ignore as e2k  # noqa: E402
KB_IGNORE = os.path.join(SCRIPTS, "kb_ignore.py")

INI = """\
[*]
*.pyc
*.json
# a global comment
untitled.docx

[gdrive/Alpha Project]
restricted-deck.pptx

[gdrive/Beta Docs]
*

[Gamma Notes]
*confidential*
"""


class ExcludeToKbIgnoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _kb_ignore_files(self):
        out = []
        for dp, _, fns in os.walk(self.root):
            if ".kb-ignore" in fns:
                out.append(os.path.relpath(os.path.join(dp, ".kb-ignore"), self.root))
        return sorted(out)

    def test_section_to_location_mapping(self):
        e2k.translate(INI, self.root)
        self.assertEqual(self._kb_ignore_files(), [
            ".kb-ignore",
            "gdrive/Alpha Project/.kb-ignore",
            "gdrive/Beta Docs/.kb-ignore",
            "gdrive/Gamma Notes/.kb-ignore",
        ])

    def test_globals_written_to_root(self):
        e2k.translate(INI, self.root)
        with open(os.path.join(self.root, ".kb-ignore")) as f:
            self.assertEqual(f.read(), "*.pyc\n*.json\nuntitled.docx\n")

    def test_bare_section_reprefixed_to_gdrive(self):
        # [Gamma Notes] (no gdrive/ prefix) -> gdrive/Gamma Notes/.kb-ignore.
        e2k.translate(INI, self.root)
        with open(os.path.join(self.root, "gdrive/Gamma Notes/.kb-ignore")) as f:
            self.assertEqual(f.read(), "*confidential*\n")

    def test_comments_and_blanks_dropped(self):
        e2k.translate(INI, self.root)
        for rel in self._kb_ignore_files():
            with open(os.path.join(self.root, rel)) as f:
                for line in f:
                    self.assertFalse(line.strip().startswith("#"),
                                     "comment leaked into %s" % rel)

    def test_empty_section_writes_no_file(self):
        ini = "[*]\n*.pyc\n\n[gdrive/Empty]\n# only a comment\n"
        e2k.translate(ini, self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root, "gdrive/Empty/.kb-ignore")))
        self.assertTrue(os.path.exists(os.path.join(self.root, ".kb-ignore")))

    def test_deny_parity_vs_ini(self):
        # The translated .kb-ignore chain must deny exactly what the INI intended.
        e2k.translate(INI, self.root)
        files = {
            "gdrive/Alpha Project/restricted-deck.pptx": "denied",
            "gdrive/Alpha Project/keep.pdf": "kept",
            "gdrive/Beta Docs/secret.docx": "denied",
            "gdrive/Gamma Notes/confidential-deck.docx": "denied",
            "gdrive/Gamma Notes/keep.docx": "kept",
            "gdrive/script.py": "kept",        # *.py NOT in this INI -> kept
            "gdrive/data.json": "denied",      # global *.json
            "gdrive/keep.md": "kept",
            "gdrive/untitled.docx": "denied",
        }
        for rel in files:
            p = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write("x")
        listing = "\n".join(files) + "\n"
        r = subprocess.run(["python3", KB_IGNORE, "filter", "--root", self.root],
                           input=listing, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        allowed = set(r.stdout.splitlines())
        for rel, expect in files.items():
            if expect == "kept":
                self.assertIn(rel, allowed, "%s should be kept" % rel)
            else:
                self.assertNotIn(rel, allowed, "%s should be denied" % rel)


if __name__ == "__main__":
    unittest.main()