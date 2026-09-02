"""Authorization policy (pure functions, no I/O).

Two dimensions, both enforced on the stack (gateway):
  - role: derived from the caller's KB_API_KEY via Open WebUI. admin overrides.
  - personal-group ownership: a personal group is implicit and only the caller
    may touch their own.

There are no shared write groups. Reads span every group that has data
(discovered from Neo4j), so cross-account knowledge is shared read-only;
writes stay per-account. This keeps one account's facts from clobbering
another's without a policy file to maintain.

group_id convention: the logical form is `user:<email>` for personal memory,
but Graphiti's group_id charset is ASCII alphanumeric, dash, and underscore
only (validate_group_id in graphiti_core, regex ^[A-Za-z0-9_-]+$). The chars
`:`, `@`, `.` are not allowed, so the stored/compared id is the sanitized
form `user-<sanitized-email>` (e.g. `user:agent@<KB_DOMAIN>` ->
`user-agent-local-test`). Client input in either form is accepted: the
boundary normalizes both to the one stored id. The caller NEVER supplies a
raw identity — only the gateway-resolved email is used, so identity is
tamper-proof.

Functions that can deny return (value, error) or (ok, error).
"""
import re

# Graphiti's group_id charset (validate_group_id in graphiti_core/helpers.py).
_GROUP_BAD = re.compile(r"[^A-Za-z0-9_-]+")


def canonical_email(email):
    """Canonicalize an email for comparison: lowercase + strip whitespace."""
    return (email or "").strip().lower()


def graphiti_group_id(group_id):
    """Normalize a group_id to Graphiti's allowed charset. Runs of disallowed
    characters become one '-'; leading/trailing '-' are stripped. Idempotent:
    an already-valid id is unchanged. Empty in -> empty out.

    Maps the logical `user:<email>` to the stored `user-<sanitized-email>`
    (e.g. `user:agent@<KB_DOMAIN>` -> `user-agent-local-test`), so client input
    in either form resolves to the one stored id used for writes, reads,
    ownership checks, and display."""
    return _GROUP_BAD.sub("-", group_id or "").strip("-")


def personal_group(email):
    """The stored personal-group id for an email. Logical form `user:<email>`;
    returned in the sanitized form Graphiti accepts (`user-<sanitized-email>`)."""
    return graphiti_group_id("user:" + canonical_email(email))


def is_admin(identity):
    return identity.get("role") == "admin"


def is_personal_group(group_id, email):
    return graphiti_group_id(group_id) == personal_group(email)


def resolve_add_group(identity, requested_group):
    """Decide the group_id for an add_memory write.
    - no requested_group -> personal group (always allowed).
    - requested_group normalizes to the caller's personal group -> allowed.
    - requested_group normalizes to another personal group (user-*) -> DENY.
    - any other requested_group -> DENY (no shared write groups).
    Client input is charset-normalized first, so `user:<email>` and the stored
    `user-<sanitized-email>` are both recognized. Returns (group_id, error)."""
    me = canonical_email(identity.get("email", ""))
    if not requested_group:
        return personal_group(me), None
    g = graphiti_group_id(requested_group.strip())
    if g == personal_group(me):
        return g, None
    if g.startswith("user-"):
        return None, "cannot write to another account's personal group"
    return None, "shared write groups are not supported; omit --group to write to your personal group"


def can_destruct(identity, group_id):
    """Authorize a destructive op on `group_id`. `group_id` is charset-normalized
    first, so client input `user:<email>` and the Neo4j-stored `user-<sanitized>`
    both map to the one id.
    - admin -> yes (override; covers any group).
    - caller's own personal group -> yes.
    - anything else -> no. Never allows a non-admin on another user's group."""
    if is_admin(identity):
        return True
    me = canonical_email(identity.get("email", ""))
    return graphiti_group_id(group_id) == personal_group(me)


def check_uuid_target_group(identity, target_group):
    """Guard for uuid-based deletes: confirm the discovered target group is one
    the caller may destruct. `target_group` is the group_id the gateway looked up
    from Neo4j for the uuid (None if not found). Returns (ok, error)."""
    if not target_group:
        return False, "target uuid not found (cannot verify ownership)"
    if not can_destruct(identity, target_group):
        return False, "not permitted to delete in group %r" % target_group
    return True, None