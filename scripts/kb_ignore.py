#!/usr/bin/env python3
"""kb_ignore -- a .gitignore-style walker exclude matcher (authoritative body).

The gateway (gateway/app.py) imports `allowed`; the gdrive-sync two-pass calls the
`filter` CLI. skills/claude/scripts/kb.py CLONES this body inline (the skill is a
monolithic deploy with no repo scripts/ on its path) -- keep the two in sync.

Semantics (gitignore, per-directory, rules relative to the .kb-ignore location):
  * patterns accumulate up the ancestor chain (shallowest first);
  * `!pattern` re-includes what an earlier pattern excluded (negation, in order;
    the LAST matching rule wins);
  * `*` does not cross `/`; `**` does; `?` = one non-`/` char;
  * a leading `/` anchors the pattern at the .kb-ignore's directory;
  * a trailing `/` (a directory pattern) matches the directory and its contents
    (B1 normalization: trailing `/` -> append `**`);
  * a no-slash name matches a file or directory of that name at any depth.

Limitation (simple initially): post-filter model -- the gitignore parent-dir rule
(a `!` in a DEEPER .kb-ignore cannot re-include a file under a directory excluded
by a SHALLOWER .kb-ignore) is NOT enforced. The `*` + `!subtree/**` allowlist (the
primary use case) works.

CLI:
  python3 kb_ignore.py filter --root <root>     stdin: root-relative paths (one
                                                per line); stdout: allowed paths.
  python3 kb_ignore.py check --root <root> --path <relpath>   exit 0=allowed, 1=denied.

API:
  allowed(root, relpath) -> bool
  clear_cache()            drop the mtime-keyed parse cache (tests / forced reload)
"""
import argparse
import os
import re
import sys

_NAME = ".kb-ignore"
# abs-dir -> (mtime, rules). mtime None when the file is absent. rules is a list
# of (negated, kind, regex) where kind is "star" (regex None), "basename", or "path".
_cache = {}


def _translate_glob(pat):
    """Translate one glob body (no leading '/') to a regex string.

    `*` -> [^/]* (within a segment); `?` -> [^/]; every other char is re.escape'd.
    `**` handling (gitignore):
      * `**/` (leading or middle, followed by '/') -> `(?:.*/)?` (zero or more
        directories, so `**/foo` matches `foo`, `a/foo`, `a/b/foo`);
      * `**` not followed by '/' (trailing, e.g. `foo/**`) -> `.*`.
    """
    out = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                if i + 2 < n and pat[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _normalize(pat):
    """B1 gitignore directory semantics. Returns (anchored, body) ready to compile.

    A trailing '/' marks a directory: `dir/` -> `dir/**` (the dir and its contents).
    """
    anchored = pat.startswith("/")
    body = pat[1:] if anchored else pat
    if body.endswith("/"):
        body = body[:-1] + "/**"
    return anchored, body


def _compile(pat):
    """Compile one .kb-ignore pattern to (negated, kind, regex|None)."""
    negated = pat.startswith("!")
    if negated:
        pat = pat[1:]
    anchored, body = _normalize(pat)
    if body in ("*", "**"):
        return (negated, "star", None)
    rx = re.compile(_translate_glob(body))
    if anchored or "/" in body:
        # Full-path match against the path relative to this .kb-ignore's directory.
        return (negated, "path", rx)
    # Basename match at any depth within the subtree.
    return (negated, "basename", rx)


def _parse_file(path):
    """Parse one .kb-ignore file to a list of compiled rules. Returns [] on absence."""
    rules = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue
                rules.append(_compile(line))
    except OSError:
        return None
    return rules


def _rules_for(dir_abs):
    """Cached rules for the .kb-ignore in dir_abs, keyed by mtime (re-read on change)."""
    p = os.path.join(dir_abs, _NAME)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = None
    cached = _cache.get(dir_abs)
    if cached and cached[0] == mtime:
        return cached[1]
    rules = _parse_file(p) if mtime is not None else []
    if rules is None:
        rules = []
    _cache[dir_abs] = (mtime, rules)
    return rules


def _rule_matches(rule, rel_to_dir):
    """Does one rule match? `rel_to_dir` = the path relative to the rule's
    .kb-ignore directory. A basename (no-slash) rule matches the basename of ANY
    file or directory component at any depth (gitignore: a directory match
    excludes its subtree)."""
    _negated, kind, rx = rule
    if kind == "star":
        return True
    if kind == "basename":
        for seg in rel_to_dir.split("/"):
            if rx.fullmatch(seg) is not None:
                return True
        return False
    return rx.fullmatch(rel_to_dir) is not None


def allowed(root, relpath):
    """True if `relpath` (relative to `root`) is NOT ignored by the ancestor
    .kb-ignore chain. Missing .kb-ignore -> allowed (no denies)."""
    rel = relpath.strip().lstrip("./")
    if rel == "":
        return True
    rel = rel.strip("/")
    parts = rel.split("/")
    root_abs = os.path.abspath(root)
    excluded = False
    # Walk root + each directory along relpath (shallowest first). At each level,
    # apply that .kb-ignore's rules in file order; the LAST matching rule wins.
    for i in range(len(parts)):
        d = root_abs if i == 0 else os.path.join(root_abs, *parts[:i])
        rules = _rules_for(d)
        if not rules:
            continue
        rel_to_dir = "/".join(parts[i:])
        for rule in rules:
            if _rule_matches(rule, rel_to_dir):
                excluded = not rule[0]  # negated -> re-include (excluded=False)
    return not excluded


def clear_cache():
    _cache.clear()


def _cli():
    ap = argparse.ArgumentParser(prog="kb_ignore", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("filter", help="filter root-relative paths (stdin->stdout)")
    pf.add_argument("--root", required=True)
    pc = sub.add_parser("check", help="check one path (exit 0=allowed, 1=denied)")
    pc.add_argument("--root", required=True)
    pc.add_argument("--path", required=True)
    args = ap.parse_args()

    if args.cmd == "check":
        sys.exit(0 if allowed(args.root, args.path) else 1)

    # filter: emit allowed paths; exit 0 on success regardless of count.
    for line in sys.stdin:
        p = line.rstrip("\n")
        if not p:
            continue
        if allowed(args.root, p):
            sys.stdout.write(p + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())