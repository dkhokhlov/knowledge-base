# KB Group Owners — Graphiti group authorization policy
#
# Personal groups `user:<email>` are implicit (never listed here). All groups
# are readable by all (read-only); this file only gates WRITES and destructive
# ops on SHARED groups. Identity is derived by the kb-gateway from the caller's
# KB_API_KEY (an Open WebUI key) — emails here must match Open WebUI account
# emails (they are canonicalized: lowercased + trimmed).
#
# To declare a shared group, add a data row under the table below, e.g.:
#   | atlas-team | alice@example.com, bob@example.com | Atlas shared memory |
#
# Rules (the gateway fails CLOSED on any violation — requests get 500, never a
# permissive empty policy):
#   - group_id must NOT start with `user:` (personal groups are implicit).
#   - owners is a non-empty, comma-separated list of valid Open WebUI emails.
#   - No duplicate group_id.
# Keep this file version-controlled and PR-reviewed. The gateway mounts it
# read-only and reloads it at startup — run `make restart` (kb-gateway) to apply.
#
# Today `owners` are Open WebUI emails. Later an LDAP/RBAC sync can render this
# file from directory groups; the gateway authorize step stays unchanged.

| group_id | owners | description |
|----------|--------|-------------|