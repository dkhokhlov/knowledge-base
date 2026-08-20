"""Authorization policy (pure functions, no I/O).

Two dimensions, both enforced on the stack (gateway):
  - role: derived from the caller's KB_API_KEY via Open WebUI. admin overrides.
  - personal-group ownership: a `user:<email>` group is implicit and only the
    caller may touch their own.

There are no shared write groups. Reads span every group that has data
(discovered from Neo4j), so cross-account knowledge is shared read-only;
writes stay per-account. This keeps one account's facts from clobbering
another's without a policy file to maintain.

group_id convention: `user:<email>` for personal memory. The caller NEVER
supplies a raw identity — only the gateway-resolved email is used, so
identity is tamper-proof.

Functions that can deny return (value, error) or (ok, error).
"""


def canonical_email(email):
    """Canonicalize an email for comparison: lowercase + strip whitespace."""
    return (email or "").strip().lower()


def personal_group(email):
    return "user:" + canonical_email(email)


def is_admin(identity):
    return identity.get("role") == "admin"


def is_personal_group(group_id, email):
    return group_id == personal_group(email)


def resolve_add_group(identity, requested_group):
    """Decide the group_id for an add_memory write.
    - no requested_group -> personal user:<email> (always allowed).
    - requested_group == caller's personal group -> allowed.
    - requested_group is another personal group (user:<other>) -> DENY.
    - any other requested_group -> DENY (no shared write groups).
    Returns (group_id, error)."""
    me = canonical_email(identity.get("email", ""))
    if not requested_group:
        return personal_group(me), None
    g = requested_group.strip()
    if g == personal_group(me):
        return g, None
    if g.startswith("user:"):
        return None, "cannot write to another account's personal group"
    return None, "shared write groups are not supported; omit --group to write to your personal group"


def can_destruct(identity, group_id):
    """Authorize a destructive op on `group_id`.
    - admin -> yes (override; covers any group).
    - caller's own personal group -> yes.
    - anything else -> no. Never allows a non-admin on user:<other>."""
    if is_admin(identity):
        return True
    me = canonical_email(identity.get("email", ""))
    return group_id == personal_group(me)


def check_uuid_target_group(identity, target_group):
    """Guard for uuid-based deletes: confirm the discovered target group is one
    the caller may destruct. `target_group` is the group_id the gateway looked up
    from Neo4j for the uuid (None if not found). Returns (ok, error)."""
    if not target_group:
        return False, "target uuid not found (cannot verify ownership)"
    if not can_destruct(identity, target_group):
        return False, "not permitted to delete in group %r" % target_group
    return True, None