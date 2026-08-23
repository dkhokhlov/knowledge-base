#!/usr/bin/env bash
# Report the status of gdrive RAG indexing: indexer container + oikb daemon
# state, OWUI KB file_count (indexed) vs the host gdrive/ allowlisted file count
# (source), the delta, and an ETA when the sync is not yet finished.
#
# ETA method: while indexed < source, sample the OWUI KB file_count over a short
# window (GDRIVE_STATUS_SAMPLE_SECS, default 6s). If it is increasing, ETA =
# remaining / (delta-indexed / sample-secs). If it is not increasing (between
# syncs or stalled), fall back to the rate observed in oikb's last successful
# sync history entry (files_added+modified / duration). If neither is available
# (cold first sync, no completed history yet), ETA is "unknown".
#
# Pending detection: file_count (above) only reflects files LINKED to the KB,
# not files whose text has been extracted. A file can be linked and still sit
# at data.status=pending if the extraction sidecar (markitdown-ocr) hasn't
# finished it yet — invisible to semantic search but also invisible to the
# indexed/source delta. GET /api/v1/knowledge/{id}/files/pending lists exactly
# these files (name, status, age since linked). A long age is NOT proof of a
# hang by itself — extraction runs one file at a time, so age also grows while
# the pipeline is legitimately busy on other files. Cross-check with
# `docker logs kb-markitdown-ocr` (recent /process activity = pipeline alive)
# before assuming a given file is wedged.
#
# Preconditions: `make gdrive-index-bootstrap` has run (GDRIVE_KB_ID + OIKB_API_KEY
# in .env.local, agent user granted read on the KB). Reads the KB with the
# read-scoped agent key (OPENWEBUI_USER_API_KEY). Reaches oikb's /health and
# /history via `docker exec` (the indexer publishes no host port).
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local
set +a

: "${GDRIVE_KB_ID:?FAIL  GDRIVE_KB_ID not set in .env.local (run: make gdrive-index-bootstrap)}"
: "${OIKB_API_KEY:?FAIL  OIKB_API_KEY not set in .env.local (run: make gdrive-index-bootstrap)}"
: "${OPENWEBUI_USER_API_KEY:?FAIL  OPENWEBUI_USER_API_KEY not set in .env.local (run: make api-keys)}"

# Container state (host docker, not the API).
CONT_STATUS="$(docker ps --filter "name=^/kb-gdrive-indexer$" --format '{{.Status}}' 2>/dev/null || true)"

python3 - "$CONT_STATUS" <<'PY'
import os, sys, json, time, subprocess, urllib.request, urllib.error, datetime

CONT_STATUS = sys.argv[1]
O = os.environ.get("KB_HOST") or ("http://localhost:%s" % os.environ.get("KB_HOST_PORT", "3000"))
UK = os.environ["OPENWEBUI_USER_API_KEY"]
KB_ID = os.environ["GDRIVE_KB_ID"]
OIKB_KEY = os.environ["OIKB_API_KEY"]
GDRIVE_DIR = "./gdrive"
SAMPLE_SECS = int(os.environ.get("GDRIVE_STATUS_SAMPLE_SECS", "6"))

# Same allowlist as .oikb.yaml (extensions, compared case-insensitively).
ALLOW = {"docx","pdf","pptx","xlsx","txt","md","html","json","log","tex"}

def http_json(url, token=None, timeout=15):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode() or "")[:200]
    except Exception as e:
        return 0, str(e)

def iso_from_epoch(ep):
    if not ep:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(float(ep), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return "-"

def fmt_eta(secs):
    if secs is None:
        return "unknown"
    secs = max(0, int(secs))
    if secs < 60:
        return "~%ds" % secs
    m, s = divmod(secs, 60)
    if m < 60:
        return "~%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    return "~%dh%02dm" % (h, m)

# --- source count: walk gdrive/, count allowlisted files --------------------
source_count = 0
if os.path.isdir(GDRIVE_DIR):
    for root, _dirs, files in os.walk(GDRIVE_DIR):
        for fn in files:
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext in ALLOW:
                source_count += 1

# --- indexed count: OWUI KB file_count (read-scoped key) --------------------
# file_count is exposed ONLY on the list endpoint (GET /api/v1/knowledge/). The
# detail endpoint (GET /api/v1/knowledge/{id}) has NEITHER a file_count field NOR
# a populated files array (it returns files: []), so reading file_count from the
# detail endpoint always yields None/0. The list endpoint, called with the
# read-scoped agent key, returns the KBs the agent can read (the gdrive KB has a
# read grant -> listed with write_access=False) and includes file_count per KB.
def indexed_count():
    st, d = http_json("%s/api/v1/knowledge/" % O, token=UK)
    if st != 200 or not isinstance(d, dict):
        return None
    items = d.get("items") or d.get("data") or []
    for k in items:
        if k.get("id") == KB_ID:
            return k.get("file_count")
    return None

# --- pending: files linked to the KB but not yet extracted -------------------
def pending_files():
    st, d = http_json("%s/api/v1/knowledge/%s/files/pending" % (O, KB_ID), token=UK)
    if st != 200 or not isinstance(d, list):
        return None
    return d

# --- oikb daemon: /health (no auth) + /history (bearer) via docker exec -----
def oikb_get(path, auth=False):
    inner = (
        "import urllib.request,json,os\n"
        "h={}\n"
        "if %r: h['Authorization']='Bearer '+os.environ.get('OIKB_API_KEY','')\n"
        "try:\n"
        "  r=urllib.request.urlopen(urllib.request.Request('http://localhost:8080%s',headers=h),timeout=10)\n"
        "  print(r.read().decode())\n"
        "except Exception as e:\n"
        "  print(json.dumps({'_error':str(e)}))\n"
    ) % (auth, path)
    env = dict(os.environ)
    if auth:
        env["OIKB_API_KEY"] = OIKB_KEY
    try:
        out = subprocess.run(["docker", "exec", "-i", "kb-gdrive-indexer", "python"],
                             input=inner, capture_output=True, text=True, timeout=15)
        if out.returncode != 0 or not out.stdout.strip():
            return {"_error": (out.stderr.strip() or "docker exec failed")[:200]}
        return json.loads(out.stdout.strip())
    except Exception as e:
        return {"_error": str(e)[:200]}

health = oikb_get("/health", auth=False)
src_state = (health.get("sources") or {}).get("/gdrive") if isinstance(health, dict) else None
if not isinstance(src_state, dict):
    src_state = {"_error": health.get("_error", "no /gdrive source in oikb /health") if isinstance(health, dict) else str(health)}
oikb_status = src_state.get("status", "?")
last_sync = iso_from_epoch(src_state.get("last_sync"))
next_sync = src_state.get("next_sync_in", "-")
oikb_errors = src_state.get("errors") or []
oikb_warnings = src_state.get("warnings") or []

hist = oikb_get("/history?limit=20", auth=True)
history_rate = None  # files/sec from last successful sync
if isinstance(hist, dict) and isinstance(hist.get("entries"), list):
    for e in reversed(hist["entries"]):
        if e.get("status") == "ok":
            dur_s = (e.get("duration_ms") or 0) / 1000.0
            done = (e.get("files_added") or 0) + (e.get("files_modified") or 0)
            if dur_s > 0 and done > 0:
                history_rate = done / dur_s
            break

# --- report -----------------------------------------------------------------
print("indexer container : %s" % (CONT_STATUS or "NOT RUNNING"))
src_note = ""
if oikb_errors:
    src_note = "  errors=%d (%s)" % (len(oikb_errors), str(oikb_errors[0]).replace("\n", " ")[:140])
elif oikb_warnings:
    src_note = "  warnings=%d" % len(oikb_warnings)
print("oikb source       : status=%s last_sync=%s next_in=%s%s" % (
    oikb_status, last_sync, next_sync, src_note))
idx0 = indexed_count()
if idx0 is None:
    print("indexed (OWUI KB) : <not visible to agent key — run: make gdrive-index-bootstrap>")
    print("source (gdrive/)  : %d allowlisted files" % source_count)
    sys.exit(0)

pending = pending_files()
if pending is None:
    print("pending (OWUI)    : <endpoint unavailable>")
elif not pending:
    print("pending (OWUI)    : 0 files awaiting extraction")
else:
    now = time.time()
    pending.sort(key=lambda f: f.get("created_at") or now)  # oldest (longest-pending) first
    print("pending (OWUI)    : %d file(s) awaiting extraction" % len(pending))
    for f in pending[:20]:
        age = now - (f.get("created_at") or now)
        status = (f.get("data") or {}).get("status", "?")
        print("  %-50s age=%-10s status=%s" % (f.get("filename", "?")[:50], fmt_eta(age), status))
    if len(pending) > 20:
        print("  ... and %d more" % (len(pending) - 20))

print("source (gdrive/)  : %d allowlisted files" % source_count)
remaining = source_count - idx0
if remaining <= 0:
    if oikb_errors:
        print("indexed (OWUI KB) : %d  |  counts match but last sync had %d error(s) — see oikb source line" % (idx0, len(oikb_errors)))
    else:
        print("indexed (OWUI KB) : %d  |  sync COMPLETE (indexed >= source)" % idx0)
    sys.exit(0)

# Not finished: sample the KB count over a short window to estimate rate.
print("indexed (OWUI KB) : %d  |  remaining=%d" % (idx0, remaining))
time.sleep(SAMPLE_SECS)
idx1 = indexed_count()
rate = None
if idx1 is not None and idx1 > idx0:
    rate = (idx1 - idx0) / SAMPLE_SECS
elif history_rate:
    rate = history_rate
    print("  (no progress in %ds window; using last-sync rate)" % SAMPLE_SECS)
if rate and rate > 0:
    eta = remaining / rate
    print("ETA               : %s  (rate=%.2f files/s, %d newly indexed in %ds)" % (
        fmt_eta(eta), rate, (idx1 - idx0) if idx1 else 0, SAMPLE_SECS))
elif oikb_status in ("ok", "partial") and idx1 is not None and idx1 == idx0:
    # Plateaued with the source not fully indexed and no progress in the sample
    # window: the remaining files are not pending. If oikb logged per-cycle
    # errors, those files are failing to link (e.g. OWUI rejects a file with
    # 400) — see the oikb source line. Otherwise the remaining are duplicate-
    # content (OWUI dedups by hash, rejects with 400) or over .oikb.yaml
    # max-size, so oikb will never link them. status is "partial" permanently in
    # either case; report done-with-skips, not an ETA.
    if oikb_errors:
        print("remaining         : not pending — %d file(s) failing to link (see errors above); the rest duplicate-content/over-max-size" % len(oikb_errors))
    else:
        print("remaining         : not pending — duplicate-content (OWUI dedups) or over max-size, skipped")
    print("ETA               : n/a (sync plateaued; no files in flight)")
else:
    print("ETA               : unknown (no observed rate yet — first sync still running)")
PY