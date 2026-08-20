"""Authorization policy (pure functions, no I/O).

Two dimensions, both enforced on the stack (gateway):
  - role: derived from the caller's KB_API_KEY via OWUI. admin overrides.
  - ownership: from OWNERS.md by canonical email. Personal groups `user:<email>`
    are implicit and only the caller may touch their own.

group_id convention: `user:<email>` for personal memory; a shared group_id from
OWNERS.md. The caller NEVER supplies a raw identity — only the gateway-resolved
email is used, so identity is tamper-proof.

All functions return (ok: bool, error: str|None).
"""
import owners as owners_mod


def personal_group(email):
    return "user:" + owners_mod.canonical_email(email)


def is_admin(identity):
    return identity.get("role") == "admin"


def is_personal_group(group_id, email):
    return group_id == personal_group(email)


def resolve_add_group(identity, owners, requested_group):
    """Decide the group_id for an add_memory write.
    - no requested_group -> personal user:<email> (always allowed).
    - requested_group == caller's personal group -> allowed (personal).
    - requested_group is another personal group (user:<other>) -> DENY (codex #12).
    - requested_group is a shared group in OWNERS.md -> caller must own it or be admin.
    - requested_group is an unknown shared group (not in OWNERS, not personal) -> DENY.
    Returns (group_id, error)."""
    me = owners_mod.canonical_email(identity.get("email", ""))
    if not requested_group:
        return personal_group(me), None
    g = requested_group.strip()
    if g.startswith("user:"):
        if g == personal_group(me):
            return g, None
        return None, "cannot write to another user's personal group"
    # Shared group.
    if g in owners:
        if is_admin(identity) or owners_mod.is_owner(owners, g, me):
            return g, None
        return None, "not an owner of group %r" % g
    return None, "unknown shared group %r (not in OWNERS policy)" % g


def can_destruct(identity, owners, group_id):
    """Authorize a destructive op on `group_id`.
    - admin -> yes (override; covers personal and shared groups).
    - caller's own personal group -> yes.
    - shared group the caller owns -> yes.
    - anything else -> no. Never allows a non-admin on user:<other>."""
    if is_admin(identity):
        return True
    me = owners_mod.canonical_email(identity.get("email", ""))
    if group_id == personal_group(me):
        return True
    if group_id in owners and owners_mod.is_owner(owners, group_id, me):
        return True
    return False


def check_uuid_target_group(identity, owners, target_group):
    """Guard for uuid-based deletes: confirm the discovered target group is one
    the caller may destruct. `target_group` is the group_id the gateway looked up
    from Neo4j for the uuid (None if not found). Returns (ok, error)."""
    if not target_group:
        return False, "target uuid not found (cannot verify ownership)"
    if not can_destruct(identity, owners, target_group):
        return False, "not permitted to delete in group %r" % target_group
    return True, None