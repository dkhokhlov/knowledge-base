#!/usr/bin/env python3
"""Translate an INI ``.exclude.conf`` / ``gdrive-exclude.conf`` into per-directory
gitignore-style ``.kb-ignore`` files (Phase 4d unification).

The OLD deny-list was ONE INI file: ``[section]`` headers are paths relative to
``./root`` (``[*]`` = globals, ``[gdrive/<drive>]`` = one drive), each listing
rclone-native patterns. The NEW deny-list is a chain of ``.kb-ignore`` files, one
per directory, gitignore-style. This script converts the INI into that chain:

  ``[*]``                    -> ``<target-root>/.kb-ignore``          (globals)
  ``[gdrive/Team Mtgs]``     -> ``<target-root>/gdrive/Team Mtgs/.kb-ignore``
  ``[Team Mtgs]``            -> ``<target-root>/gdrive/Team Mtgs/.kb-ignore``  (re-prefixed)

Patterns are copied VERBATIM. The rclone-native and gitignore semantics agree for
every pattern class the INI allows: no-slash = basename at any depth; a slash
pattern = anchored at the section root (= the .kb-ignore's own dir); a leading
``/`` = anchor at that dir; ``*`` does not cross ``/``, ``**`` does. So no pattern
rewriting is needed -- only the section-header -> file-location mapping changes.

CLI:  python3 exclude_to_kb_ignore.py --src <ini-file> --target-root <dir>
       (or pipe the INI on stdin with only --target-root)

Module:  translate(ini_text, target_root) -> list[str] of written .kb-ignore paths.

Used by ``tests/conftest.py`` (the at-scale iso fixture copies the live deny-list into the
throwaway clone). The live ``.exclude.conf`` is gitignored (Drive file paths are
business-sensitive); this script never prints patterns."""
import argparse
import os
import sys


def translate(ini_text, target_root):
    """Parse INI sections + write per-dir .kb-ignore files under target_root.
    Returns the list of written .kb-ignore paths (relative to target_root)."""
    sections = {}   # relpath -> list[str] lines; "" = globals (root/.kb-ignore)
    order = []      # preserve first-seen section order
    cur = None
    for line in ini_text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]") and len(s) > 2:
            h = s[1:-1].strip()
            if h == "*":
                key = ""                       # globals -> root/.kb-ignore
            elif h.startswith("gdrive/"):
                key = h                        # already gdrive/-prefixed
            else:
                key = "gdrive/" + h            # re-prefix a bare [X] (gdrive-exclude.conf)
            if key not in sections:
                sections[key] = []
                order.append(key)
            cur = key
            continue
        if cur is None:
            continue                           # lines before the first section: drop
        sections[cur].append(line)

    written = []
    for key in order:
        # Keep only patterns (drop comments + blank lines). An inter-section
        # comment in the INI sits between two headers; a single pass would attach
        # it to the PREVIOUS section (a leak), so drop all comments -- the .kb-ignore
        # files are pattern lists, and the format docs live in docs/operations.md.
        patterns = [l for l in sections[key]
                    if l.strip() and not l.strip().startswith("#")]
        if not patterns:
            continue                           # comments-only / empty -> no file
        if key == "":
            rel = ".kb-ignore"
            path = os.path.join(target_root, ".kb-ignore")
        else:
            parts = key.split("/") + [".kb-ignore"]
            rel = "/".join(parts)
            path = os.path.join(target_root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(patterns) + "\n")
        written.append(rel)
    return written


def main():
    ap = argparse.ArgumentParser(
        prog="exclude_to_kb_ignore",
        description="Translate an INI .exclude.conf into per-directory .kb-ignore files.")
    ap.add_argument("--src", help="INI source file (default: stdin)")
    ap.add_argument("--target-root", required=True,
                    help="write .kb-ignore files under this root (e.g. ./root)")
    args = ap.parse_args()
    if args.src:
        with open(args.src, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    written = translate(text, args.target_root)
    for rel in written:
        print(rel)


if __name__ == "__main__":
    sys.exit(main() or 0)