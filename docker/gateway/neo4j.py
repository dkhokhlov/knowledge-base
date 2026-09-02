"""Neo4j HTTP transactional client (stdlib urllib + basic auth).

Used for:
  - group discovery: the live source of truth for which group_ids have data
    (replaces a roster file). Graphiti's own get_community_clusters pattern.
  - group-lookup guard for uuid-based deletes: before deleting an edge or
    episode by uuid, confirm the target's stored group_id belongs to a group
    the caller owns (fail-closed if not found).

No pip dependency. Talks to the Neo4j HTTP transactional endpoint over the
container-internal `graph_internal` network only (never published to the host).
"""
import base64
import json
import os
import urllib.error
import urllib.request


class Neo4jError(Exception):
    """Raised on transport error or a Neo4j-side error in the response."""


def _endpoint():
    base = os.environ.get("NEO4J_URL", "http://neo4j:7474").rstrip("/")
    db = os.environ.get("NEO4J_DATABASE", "neo4j")
    return "%s/db/%s/tx/commit" % (base, db)


def _auth_header():
    user = os.environ.get("NEO4J_USER", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD", "password")
    token = "%s:%s" % (user, pwd)
    return "Basic " + base64.b64encode(token.encode()).decode()


def _query(statement, params=None):
    """Run one Cypher statement via the transactional endpoint. Returns the
    first result row list (or [] if no rows). Raises Neo4jError on any failure
    (transport error, non-200, or a Neo4j `errors` array in the body)."""
    body = json.dumps({"statements": [{"statement": statement, "parameters": params or {}}]})
    req = urllib.request.Request(
        _endpoint(), data=body.encode(),
        headers={"Content-Type": "application/json", "Authorization": _auth_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(os.environ.get("NEO4J_TIMEOUT", "10"))) as r:
            txt = r.read().decode()
    except urllib.error.HTTPError as e:
        raise Neo4jError("Neo4j HTTP %s: %s" % (e.code, (e.read().decode() or "")[:200]))
    except urllib.error.URLError as e:
        raise Neo4jError("Neo4j unreachable: %s" % e)
    try:
        data = json.loads(txt)
    except Exception:
        raise Neo4jError("Neo4j returned non-JSON: %s" % txt[:200])
    if data.get("errors"):
        raise Neo4jError("Neo4j error: %s" % json.dumps(data["errors"])[:300])
    results = data.get("results") or []
    if not results:
        return []
    rows = results[0].get("data") or []
    if not rows:
        return []
    return rows[0].get("row", [])


def discover_groups():
    """Return all distinct group_ids that currently have data (Entity or
    Episodic nodes). Source of truth for the read-all search scope."""
    stmt = (
        "MATCH (n) WHERE n.group_id IS NOT NULL AND (n:Entity OR n:Episodic) "
        "RETURN collect(DISTINCT n.group_id) AS groups"
    )
    row = _query(stmt)
    if not row:
        return []
    val = row[0]
    return val if isinstance(val, list) else []


def lookup_node_group(uuid):
    """Return the group_id of an Episodic node by uuid, or None if not found.
    Used to guard delete_episode."""
    row = _query(
        "MATCH (n:Episodic) WHERE n.uuid = $uuid RETURN n.group_id AS g LIMIT 1",
        {"uuid": uuid},
    )
    if not row:
        return None
    return row[0]


def lookup_edge_group(uuid):
    """Return the group_id of an entity edge by uuid, or None if not found.
    Label-agnostic (Graphiti relationship edges carry uuid + group_id). Used
    to guard delete_entity_edge."""
    row = _query(
        "MATCH ()-[r]->() WHERE r.uuid = $uuid RETURN r.group_id AS g LIMIT 1",
        {"uuid": uuid},
    )
    if not row:
        return None
    return row[0]