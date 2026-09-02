"""Graphiti REST client (stdlib urllib).

Talks to the zepai/graphiti REST server (graph_service FastAPI app) over the
container-internal `graph_internal` network only. Replaces the MCP transport:
MCP could not be safely exposed (security), and its bundled LLM factory
hardcodes OpenAIClient (OpenAI Responses API), which is incompatible with
Ollama (extraction silently stored nothing). The REST server is started by
graphiti/bootstrap.py, which injects OpenAIGenericClient (Chat Completions) +
OpenAIEmbedder(nomic-embed-text, 768) so Ollama extraction works.

Endpoint map (zepai/graphiti:0.22.0):
  add_memory      POST   /messages                  (202, async extraction)
  search_facts    POST   /search                    -> {facts:[FactResult]}
  get_episodes    GET    /episodes/{g}?last_n=N      (single-group; loop+merge)
  clear_group     DELETE /group/{g}
  delete_edge     DELETE /entity-edge/{uuid}
  delete_episode  DELETE /episode/{uuid}
  status          GET    /healthcheck               -> {status:"healthy"}

`/messages` is async: 202 only proves the episode was queued, not that facts
were extracted. Callers that need proof must poll /search (the gateway's
test_06 does this). `status`/`/healthcheck` proves the FastAPI process is up;
Neo4j health is the gateway's own neo4j.py check, not this endpoint.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class GraphitiError(Exception):
    """Transport failure or a non-2xx from the Graphiti REST server (-> 502)."""


def _base():
    return os.environ.get("GRAPHITI_URL", "http://graphiti:8000").rstrip("/")


def _timeout():
    return float(os.environ.get("GRAPHITI_TIMEOUT", "60"))


def _request(method, path, body=None):
    """Issue a JSON request; return (status, parsed_json_or_None). Raises
    GraphitiError on transport failure or non-2xx."""
    url = _base() + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            raw = r.read().decode()
            return r.status, _parse(raw)
    except urllib.error.HTTPError as e:
        # 4xx/5xx from Graphiti (404 on unknown uuid, 422 on bad payload) ->
        # 502 to the client; the gateway pre-checks ownership so 404 should
        # not normally occur.
        body_txt = ""
        try:
            body_txt = e.read().decode() or ""
        except Exception:
            pass
        raise GraphitiError("Graphiti HTTP %s: %s" % (e.code, body_txt[:200]))
    except urllib.error.URLError as e:
        raise GraphitiError("Graphiti unreachable: %s" % e)


def _parse(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        raise GraphitiError("non-JSON Graphiti response: %s" % raw[:200])


def add_memory(group_id, name, text, source_description):
    """Queue an episode for async extraction. Returns None on 2xx (202).
    The REST /messages DTO requires role_type (Literal user|assistant|system)
    and role (required-but-nullable); name + source_description default to ''."""
    body = {
        "group_id": group_id,
        "messages": [{
            "content": text,
            "role_type": "user",
            "role": None,
            "name": name or "",
            "source_description": source_description or "",
        }],
    }
    _request("POST", "/messages", body)


def search_facts(group_ids, query, max_facts):
    """Search facts across the given groups (read-all scope). Returns the
    facts list (FactResult dicts: uuid, name, fact, valid_at, ...)."""
    _, data = _request("POST", "/search", {
        "group_ids": group_ids,
        "query": query,
        "max_facts": max_facts,
    })
    if not isinstance(data, dict):
        return []
    facts = data.get("facts")
    return facts if isinstance(facts, list) else []


def get_episodes(group_ids, max_episodes):
    """Return episodes across all groups, merged + globally capped.

    REST /episodes/{group_id} is single-group with a required `last_n`. Looping
    over groups yields up to `last_n` PER group (max*N), so merge, sort by
    created_at, then apply one global `max_episodes` cap (the old MCP tool ran
    a single global query returning at most `max`). Group ids are URL-encoded
    (legacy ids may contain ':', '@', '.')."""
    merged = []
    for g in group_ids or []:
        try:
            _, data = _request("GET", "/episodes/%s?last_n=%d" % (
                urllib.parse.quote(g, safe=""), max_episodes))
        except GraphitiError:
            continue  # one group failing must not break the read-all view
        if isinstance(data, list):
            merged.extend(data)
    merged.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return merged[:max_episodes]


def clear_group(group_id):
    """Delete all nodes/edges/episodes for a group (forget)."""
    _request("DELETE", "/group/%s" % urllib.parse.quote(group_id, safe=""))


def delete_edge(uuid):
    _request("DELETE", "/entity-edge/%s" % urllib.parse.quote(uuid, safe=""))


def delete_episode(uuid):
    _request("DELETE", "/episode/%s" % urllib.parse.quote(uuid, safe=""))


def status():
    """Graphiti server process health (/healthcheck). Neo4j health is checked
    separately by the gateway via neo4j.py; this only proves the FastAPI app."""
    _, data = _request("GET", "/healthcheck")
    return data if isinstance(data, dict) else {"status": "unknown"}