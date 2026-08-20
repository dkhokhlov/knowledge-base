"""OWNERS.md policy parser — fail-closed.

OWNERS.md is a markdown table mapping a shared group_id to its owner emails:

    | group_id   | owners     | description               |
    |------------|------------|---------------------------|
    | atlas-team | alice, bob | Project Atlas shared memory |

Personal groups `user:<email>` are implicit (never listed). All groups are
readable by all (read-only); ownership only gates writes + destructive ops.

Parse rules (fail-closed — any violation raises ParseError, which the gateway
turns into HTTP 500, never a permissive empty policy):
  - Exactly three columns: group_id, owners, description.
  - group_id is non-empty and NOT a personal group (must not start "user:").
  - owners is a non-empty comma-separated list of valid-looking emails.
  - Emails are canonicalized (lowercase, strip).
  - Duplicate group_id is an error.
"""
import os


class ParseError(Exception):
    """OWNERS.md is malformed or violates policy. Gateway maps this to 500."""


def canonical_email(email):
    """Canonicalize an email for comparison: lowercase + strip whitespace."""
    return (email or "").strip().lower()


def _looks_like_email(s):
    s = (s or "").strip()
    return "@" in s and " " not in s and s.index("@") > 0 and s.index("@") < len(s) - 1


def _split_row(line):
    """Split a markdown table row `| a | b | c |` into stripped cell values.
    Returns None if the line is not a table row."""
    s = line.strip()
    if not s.startswith("|") or not s.endswith("|"):
        return None
    cells = [c.strip() for c in s[1:-1].split("|")]
    if len(cells) != 3:
        return None
    return cells


def load_owners(path):
    """Parse OWNERS.md into {group_id: {"owners": [email,...], "description": str}}.

    A file with a recognized header + separator but zero data rows is a valid
    empty policy (no shared groups; only personal groups exist). Any deviation
    inside the table — wrong column count, duplicate group, a personal-group
    entry, an invalid email, or a row before the separator — raises ParseError
    (fail-closed). A missing file also raises (the gateway cannot authorize
    shared-group writes without a policy)."""
    if not path or not os.path.exists(path):
        raise ParseError("OWNERS file not found: %s" % path)
    with open(path) as f:
        lines = f.readlines()

    owners = {}
    header_seen = False
    in_table = False
    for ln in lines:
        stripped = ln.strip()
        if not in_table and not stripped.startswith("|"):
            continue  # prose/comment lines before the table
        cells = _split_row(ln)
        if cells is None:
            if in_table:
                # A pipe line with the wrong column count inside the table is
                # malformed, not ignorable.
                raise ParseError("malformed table row (expected 3 cells): %r" % ln)
            continue
        lower0 = cells[0].lower()
        if not header_seen:
            if "group_id" in lower0 and "owner" in cells[1].lower():
                header_seen = True
                continue
            continue  # still before the header
        if set(cells[0]) <= set("-: "):  # separator row
            in_table = True
            continue
        if not in_table:
            raise ParseError("data row before table separator: %r" % ln)
        group_id, owners_cell, description = cells
        group_id = group_id.strip()
        if not group_id:
            raise ParseError("empty group_id in row: %r" % ln)
        if group_id.startswith("user:"):
            raise ParseError(
                "personal group %r must not appear in OWNERS policy (personal "
                "groups are implicit)" % group_id)
        if group_id in owners:
            raise ParseError("duplicate group_id %r" % group_id)
        raw_owners = [o for o in (x.strip() for x in owners_cell.split(",")) if o]
        if not raw_owners:
            raise ParseError("group %r has no owners" % group_id)
        canon = []
        for o in raw_owners:
            if not _looks_like_email(o):
                raise ParseError("group %r has invalid owner email %r" % (group_id, o))
            canon.append(canonical_email(o))
        owners[group_id] = {"owners": canon, "description": description.strip()}
    return owners


def is_owner(owners, group_id, email):
    """True if `email` (canonical) is an owner of shared `group_id`."""
    entry = owners.get(group_id)
    if not entry:
        return False
    return canonical_email(email) in entry["owners"]