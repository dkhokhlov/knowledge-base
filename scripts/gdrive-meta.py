#!/usr/bin/env python3
"""gdrive file `.meta` sidecar generator (read-only).

For each Google Drive file that is mirrored under ./gdrive, read its Drive file
details (description, owners, times, size, ...) plus any Drive Approval, and write
two per-file sidecars next to the source file:
  `<local-path>.meta`      YAML, human/audit record (multiline description).
  `<local-path>.meta.json` JSON, the machine copy the api-gateway reads at upload
                            time and passes into OWUI File.meta.data (the gateway is
                            zero-dependency stdlib, so it reads JSON, not YAML).
Both carry `description`, a `labels` list parsed from `[...]` markers in the
description, a `grounded` convenience flag, the Drive file attributes, and a
structured `approval` field when the file has one.

The sidecars are NOT indexed: `.meta` is dropped by the gateway document allowlist
(`meta` not in gateway/app.py DEFAULT_ALLOW), and `.meta.json` (ext `json`, which IS
allowed) is skipped by name in `_entry_for`. Both are protected from rclone sync
deletion (gdrive-exclude.conf [*] `*.meta` + `*.json`). Source files keep indexing.

Read-only: reuses the existing authenticated rclone `gdrive` remote for the access
token (rclone stays the owner of token refresh) and the file id<->path map. No Drive
writes, no new OAuth flow. Tokens/secrets are never printed or logged.

Usage:
    ./scripts/gdrive-meta                 # all mirrored shared drives
    ./scripts/gdrive-meta --drive <drive-name>
    ./scripts/gdrive-meta --file <drive-file-id>
    ./scripts/gdrive-meta --dry-run        # print planned sidecars, write nothing

Exit codes: 0 success (sidecars written or dry-run); 1 on any Drive/rclone error or
write failure; 2 on a bad Drive HTTP status (auth/scope/not-found) that aborts early.
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time

import requests
import yaml

# --- repo root (run from anywhere) -------------------------------------------
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GDRIVE_DIR = os.path.join(REPO, "gdrive")
RCLONE_REMOTE = "gdrive"  # the authenticated remote (scripts/gdrive-sync default)
DRIVE_API = "https://www.googleapis.com/drive/v3"

# Drive file resource fields we record (kept lean; mirrors the .meta schema).
FILE_FIELDS = ("id,name,description,createdTime,modifiedTime,mimeType,originalFilename,"
               "fullFileExtension,size,md5Checksum,trashed,parents,"
               "owners(displayName,emailAddress)")

LABEL_RE = re.compile(r"\[([^\[\]]+)\]")

# Only generate .meta for files the gateway actually indexes. MUST match
# DEFAULT_ALLOW in gateway/app.py (and allowed_re in scripts/gdrive-sync) — keep in
# sync. The .meta is meaningful for documents (description/labels/approval/comments),
# not for ~2000 model-weight/binary blobs in the mirror.
DOC_EXTS = {"docx", "pdf", "pptx", "xlsx", "txt", "md", "html", "json", "log", "tex"}

# Drive caps the file `description` field at 25,000 chars; cap the recorded value to
# match (defensive — a read never exceeds it, but the .meta field is bounded).
MAX_DESCRIPTION = 25000


class _BlockStr(str):
    """str subclass whose YAML rendering is always a literal block (`|`), so the
    `description` field is always a multi-line YAML value (readable + editable),
    even when the Drive description is a single line."""
    pass


class _MetaDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings as YAML literal blocks (`|`) so
    a multiline Drive `description` stays readable in the .meta sidecar instead of
    being collapsed into a single line of escaped \\n. Single-line strings use the
    default scalar style. The `description` field uses _BlockStr to force `|`."""
    pass


def _str_representer(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


def _blockstr_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


_MetaDumper.add_representer(str, _str_representer)
_MetaDumper.add_representer(_BlockStr, _blockstr_representer)

log = logging.getLogger("gdrive-meta")


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _run(cmd, **kw):
    """Run a command, return stdout bytes. Raise RuntimeError on non-zero."""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    if p.returncode != 0:
        raise RuntimeError("command failed (%d): %s\n%s"
                           % (p.returncode, " ".join(cmd),
                              (p.stderr.decode(errors="replace") or "").strip()))
    return p.stdout


def _access_token():
    """Return a fresh Drive access token from the rclone gdrive remote.

    rclone stays the owner of refresh: a trivial `rclone lsf` first forces rclone to
    refresh + persist its token, then we read only the `token` field via the rclone rc
    loopback. The token is returned to the caller and never printed/logged.
    """
    # Force rclone to refresh its cached token (discard the listing output).
    _run(["rclone", "lsf", RCLONE_REMOTE + ":", "--max-depth", "1"], timeout=120)
    out = _run(["rclone", "rc", "--loopback", "config/get", "name=" + RCLONE_REMOTE],
               timeout=30)
    cfg = json.loads(out)
    tok = cfg.get("token")
    if not tok:
        raise RuntimeError("rclone remote %r has no token field (re-run: rclone config "
                           "reconnect %s:)" % (RCLONE_REMOTE, RCLONE_REMOTE + ":"))
    blob = json.loads(tok)
    access = blob.get("access_token")
    if not access:
        raise RuntimeError("rclone remote %r token has no access_token" % RCLONE_REMOTE)
    return access


def _list_drives():
    """Return [{id, name}] of shared drives visible to the rclone remote."""
    out = _run(["rclone", "backend", "--json", "drives", RCLONE_REMOTE + ":"], timeout=60)
    drives = json.loads(out)
    return [{"id": d.get("id"), "name": d.get("name")} for d in drives
            if d.get("id") and d.get("name")]


def _lsjson_files(drive_id):
    """rclone lsjson -R for one shared drive -> [{Path,Name,ID,Size,MimeType,ModTime}].

    `Path` is Drive-relative (e.g. 'Subdir/Example_Document.pdf'); the local
    mirror path is gdrive/<drive_name>/<Path>. Files only (dirs pruned).
    """
    out = _run(["rclone", "lsjson", "-R", RCLONE_REMOTE + ":",
                "--drive-team-drive", drive_id, "--files-only"], timeout=300)
    rows = json.loads(out)
    files = []
    for r in rows:
        if r.get("IsDir"):
            continue
        files.append({"path": r.get("Path") or "", "name": r.get("Name") or "",
                      "id": r.get("ID") or "", "size": r.get("Size"),
                      "mime": r.get("MimeType"), "modtime": r.get("ModTime")})
    return files


def _drive_file_attrs(token, drive_id):
    """Drive API files.list for one shared drive -> {file_id: attrs dict}.

    One paginated call per drive (not per file). Includes description + owners + times.
    """
    headers = {"Authorization": "Bearer " + token}
    params = {"corpora": "drive", "driveId": drive_id,
              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
              "q": "trashed=false", "pageSize": "1000",
              "fields": "nextPageToken,files(%s)" % FILE_FIELDS}
    out = {}
    page = None
    while True:
        if page:
            params["pageToken"] = page
        r = requests.get(DRIVE_API + "/files", headers=headers, params=params, timeout=60)
        if r.status_code != 200:
            _raise_drive_error("files.list", r, drive_id)
        data = r.json()
        for f in data.get("files") or []:
            if f.get("id"):
                out[f["id"]] = f
        page = data.get("nextPageToken")
        if not page:
            break
    return out


def _file_approval(token, file_id):
    """GET /drive/v3/files/{id}/approvals -> the latest approval item, or None.

    Read-only with drive.readonly. The approvals endpoint does NOT accept
    supportsAllDrives and returns only `kind` without an explicit `fields` param,
    so we request the full item projection. Returns the approval with the highest
    modifyTime.
    """
    headers = {"Authorization": "Bearer " + token}
    fields = ("items(approvalId,targetFileId,createTime,modifyTime,completeTime,"
              "status,initiator(displayName,emailAddress),"
              "reviewerResponses(reviewer(displayName,emailAddress),response),"
              "fileContentChangeBehavior)")
    r = requests.get(DRIVE_API + "/files/%s/approvals" % file_id,
                     headers=headers, params={"fields": fields}, timeout=30)
    if r.status_code == 404:
        return None  # no approvals / not applicable
    if r.status_code != 200:
        _raise_drive_error("approvals", r, file_id)
    items = (r.json() or {}).get("items") or []
    if not items:
        return None
    latest = max(items, key=lambda a: a.get("modifyTime") or "")
    return latest


def _raise_drive_error(op, r, ctx):
    """Raise a sanitized error for a bad Drive HTTP status. Never includes the token."""
    code = r.status_code
    try:
        msg = (r.json().get("error", {}).get("message") or r.text or "")
    except Exception:
        msg = r.text or ""
    msg = (msg or "").strip()[:300]
    if code == 401:
        raise RuntimeError("Drive %s auth failure (401) for %s: %s "
                           "(token may be expired; re-run: rclone config reconnect %s:)"
                           % (op, ctx, msg, RCLONE_REMOTE + ":"))
    if code == 403:
        raise RuntimeError("Drive %s permission/scope failure (403) for %s: %s"
                           % (op, ctx, msg))
    if code == 404:
        raise RuntimeError("Drive %s not found (404) for %s: %s" % (op, ctx, msg))
    raise RuntimeError("Drive %s HTTP %d for %s: %s" % (op, code, ctx, msg))


def _labels(description):
    """Unique bracket-marker labels from the description, in first-seen order."""
    seen = []
    for m in LABEL_RE.findall(description or ""):
        if m not in seen:
            seen.append(m)
    return seen


def _owners(attrs):
    return [o.get("emailAddress") or o.get("displayName")
            for o in (attrs.get("owners") or [])
            if o.get("emailAddress") or o.get("displayName")]


def _approval_record(apv):
    """Shape the Drive approval item into the .meta `approval` mapping."""
    if not apv:
        return None
    init = apv.get("initiator") or {}
    return {
        "status": apv.get("status"),
        "approval_id": apv.get("approvalId"),
        "create_time": apv.get("createTime"),
        "complete_time": apv.get("completeTime"),
        "initiator": init.get("emailAddress") or init.get("displayName"),
        "reviewers": [
            {"email": (rr.get("reviewer") or {}).get("emailAddress")
                       or (rr.get("reviewer") or {}).get("displayName"),
             "response": rr.get("response")}
            for rr in (apv.get("reviewerResponses") or [])
        ],
    }


def _person(p):
    """email if present else displayName (Drive often omits email in shared drives)."""
    p = p or {}
    return p.get("emailAddress") or p.get("displayName")


def _reply_record(r):
    return {
        "id": r.get("id"),
        "author": _person(r.get("author")),
        "content": r.get("content"),
        "created_time": r.get("createdTime"),
        "modified_time": r.get("modifiedTime"),
    }


def _comment_record(c):
    return {
        "id": c.get("id"),
        "author": _person(c.get("author")),
        "content": c.get("content"),
        "created_time": c.get("createdTime"),
        "modified_time": c.get("modifiedTime"),
        "replies": [_reply_record(r) for r in (c.get("replies") or [])],
    }


def _file_comments(token, file_id):
    """GET /drive/v3/files/{id}/comments -> [comment records] (read-only).

    Paginated; includeDeleted=false. Each record carries id, author, content,
    timestamps, and replies. Empty list when the file has no comments.
    """
    headers = {"Authorization": "Bearer " + token}
    fields = ("nextPageToken,comments(id,createdTime,modifiedTime,"
              "author(displayName,emailAddress),content,"
              "replies(id,createdTime,modifiedTime,author(displayName,emailAddress),"
              "content))")
    params = {"fields": fields, "includeDeleted": "false", "pageSize": "100"}
    out = []
    page = None
    while True:
        if page:
            params["pageToken"] = page
        r = requests.get(DRIVE_API + "/files/%s/comments" % file_id,
                         headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            _raise_drive_error("comments", r, file_id)
        data = r.json()
        for c in data.get("comments") or []:
            out.append(_comment_record(c))
        page = data.get("nextPageToken")
        if not page:
            break
    return out


def _meta_dict(drive, lf, attrs, approval, comments):
    """Build the .meta content dict from the lsjson row + Drive attrs + approval +
    comments."""
    a = attrs or {}
    raw_desc = a.get("description") or ""
    truncated = len(raw_desc) > MAX_DESCRIPTION
    desc = raw_desc[:MAX_DESCRIPTION]
    labels = _labels(raw_desc)
    meta = {
        "id": a.get("id") or lf["id"],
        "name": a.get("name") or lf["name"],
        "drive_id": drive["id"],
        "drive_name": drive["name"],
        "path": "%s/%s" % (drive["name"], lf["path"]),
        "mime_type": a.get("mimeType") or lf.get("mime"),
        # `description` is a YAML literal block (multiline); capped at MAX_DESCRIPTION.
        "description": _BlockStr(desc) if desc else "",
        "labels": labels,
        # `grounded` is the convenience flag the KB queries: True when the
        # `grounded` bracket-marker label is present in the Drive description.
        "grounded": "grounded" in labels,
        "trashed": bool(a.get("trashed")),
        "created_time": a.get("createdTime"),
        "modified_time": a.get("modifiedTime") or lf.get("modtime"),
        "size": a.get("size") if a.get("size") is not None else lf.get("size"),
        "md5_checksum": a.get("md5Checksum"),
        "owners": _owners(a),
        "parents": a.get("parents") or [],
        "approval": _approval_record(approval),
        "comments": comments,
    }
    if truncated:
        meta["description_truncated"] = True
    return meta


def _write_sidecar(local_path, meta, dry_run):
    """Write two sidecars next to the source file:
      `<path>.meta`      - YAML, human/audit record (multiline description).
      `<path>.meta.json` - JSON, the machine copy the api-gateway reads at upload
                           time (gateway is zero-dependency stdlib: no PyYAML) and
                           passes into OWUI File.meta.data.
    Same content, two formats. Both are excluded from the index walk and protected
    from rclone sync deletion (gdrive-exclude.conf [*] *.meta + *.json)."""
    sidecar = local_path + ".meta"
    json_sidecar = local_path + ".meta.json"
    text = yaml.dump(meta, Dumper=_MetaDumper, sort_keys=False,
                     allow_unicode=True, width=1000)
    jtext = json.dumps(meta, ensure_ascii=False, indent=2)
    if dry_run:
        print("--- %s" % sidecar)
        print(text)
        print("--- %s" % json_sidecar)
        print(jtext)
        return
    for path, body in ((sidecar, text), (json_sidecar, jtext)):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate gdrive .meta sidecars (read-only).")
    ap.add_argument("--drive", action="append", default=[],
                    help="shared-drive name to process (repeatable); default = all mirrored")
    ap.add_argument("--file", help="only this Drive file id")
    ap.add_argument("--dry-run", action="store_true",
                    help="print planned sidecars (with content), write nothing")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%SZ")
    # force UTC (basicConfig uses local tz by default for asctime)
    logging.Formatter.converter = time.gmtime

    if not os.path.isdir(GDRIVE_DIR):
        log.error("gdrive mirror not found: %s (run: make gdrive-sync)", GDRIVE_DIR)
        return 1

    try:
        token = _access_token()
    except RuntimeError as e:
        log.error("token: %s", e)
        return 2

    drives = _list_drives()
    wanted = set(args.drive)
    n_drives = n_files = n_written = n_absent = n_nondoc = 0
    for drive in drives:
        dname = drive["name"]
        if wanted and dname not in wanted:
            continue
        local_drive_dir = os.path.join(GDRIVE_DIR, dname)
        if not os.path.isdir(local_drive_dir):
            log.info("skip drive %r: not mirrored under gdrive/", dname)
            continue
        n_drives += 1
        log.info("drive %s (%s)", dname, drive["id"])
        try:
            lfiles = _lsjson_files(drive["id"])
            attrs_map = _drive_file_attrs(token, drive["id"])
        except RuntimeError as e:
            log.error("drive %s: %s", dname, e)
            return 1
        for lf in lfiles:
            if args.file and lf["id"] != args.file:
                continue
            # Scope to document types the gateway indexes (DOC_EXTS == DEFAULT_ALLOW
            # in gateway/app.py). Model weights/binaries have no Drive description or
            # approval signal worth a sidecar.
            ext = (lf["name"].rsplit(".", 1)[-1].lower() if "." in lf["name"] else "")
            if ext not in DOC_EXTS:
                n_nondoc += 1
                continue
            local_path = os.path.join(local_drive_dir, lf["path"])
            if not os.path.isfile(local_path):
                n_absent += 1
                continue
            n_files += 1
            try:
                apv = _file_approval(token, lf["id"])
                comments = _file_comments(token, lf["id"])
            except RuntimeError as e:
                log.error("meta %s: %s", lf["id"], e)
                return 1
            meta = _meta_dict(drive, lf, attrs_map.get(lf["id"]), apv, comments)
            _write_sidecar(local_path, meta, args.dry_run)
            n_written += 1
            log.info("  %s%s  labels=%s approval=%s comments=%d",
                     lf["path"], " [dry-run]" if args.dry_run else "",
                     meta["labels"], (meta["approval"] or {}).get("status"),
                     len(comments))
    print("gdrive-meta: drives=%d files=%d written=%d absent=%d non_doc=%d dry_run=%s"
          % (n_drives, n_files, n_written, n_absent, n_nondoc, args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())