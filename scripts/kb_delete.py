"""Operator CLI: delete ONE Open WebUI KB by id.

Admin/operator action (NOT in the /kb skill -- that is agent-scoped, user-role,
read-only). Uses an admin-role OPENWEBUI_ADMIN_API_KEY; keep it private.

Output is pretty JSON on stdout in ALL cases (agentic-first -- read by agents,
not humans). Exit 0 only on a successful delete; exit 2 on every other outcome
(refused / not_found / error). No free-text log lines; the JSON carries the
message.

Flow:
  1. require --kb <id>; missing -> error/missing_kb_id.
  2. require OPENWEBUI_ADMIN_API_KEY + KB_HOST (or --base); missing -> error.
  3. get_kb: 404 -> not_found (nothing deleted); other failure -> error.
  4. kb_inflight guard (kb_id only, no dir): pending+processing > 0 ->
     refused/drain_in_flight (delete NOT called); scan unavailable (non-200 /
     transport) -> refused/verify_unavailable (fail-closed; delete NOT called).
  5. delete_kb: success -> deleted; failure -> error/delete_failed.

Irreversible, no backup (explicit operator op -- unlike PRUNE_KB=1 make
kb-check which backs up first). Residual vectors (OWUI wraps
delete_collection in try/except: pass) surface as class 5b on the next
make kb-check. For batch, loop in shell.
"""

import argparse
import json
import os
import sys

import kb_utils  # qualified calls so tests patch kb_utils.* (not bound names)


def _emit(obj, code):
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    return code


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="kb_delete",
        description="Delete ONE Open WebUI KB by id (admin; irreversible). "
                    "Refuses if the KB has an in-flight index drain. "
                    "Emits pretty JSON in all cases.")
    p.add_argument("--kb", default="", help="KB id (UUID); get it from: "
                   "make kb-status or /kb kbs")
    p.add_argument("--base", default=None, help="OWUI/gateway base URL "
                   "(default: $KB_HOST)")
    args = p.parse_args(argv)

    kb_id = (args.kb or "").strip()
    if not kb_id:
        return _emit({"action": "delete", "kb_id": None, "status": "error",
                      "reason": "missing_kb_id",
                      "message": "KB=<id> required (get it from: make kb-status "
                                 "or /kb kbs)"}, 2)

    admin_key = os.environ.get("OPENWEBUI_ADMIN_API_KEY", "")
    if not admin_key:
        return _emit({"action": "delete", "kb_id": kb_id, "status": "error",
                      "reason": "missing_admin_key",
                      "message": "OPENWEBUI_ADMIN_API_KEY unset (run: make api-keys)"}, 2)

    base = args.base or os.environ.get("KB_HOST", "")
    if not base:
        return _emit({"action": "delete", "kb_id": kb_id, "status": "error",
                      "reason": "missing_kb_host",
                      "message": "KB_HOST unset (see .env.template)"}, 2)

    # 3. lookup (for the name + not-found; the guard does NOT use the name)
    try:
        kb = kb_utils.get_kb(base, admin_key, kb_id)
    except Exception as e:
        return _emit({"action": "delete", "kb_id": kb_id, "status": "error",
                      "reason": "lookup_failed", "message": str(e)}, 2)
    if kb is None:
        return _emit({"action": "delete", "kb_id": kb_id, "status": "not_found",
                      "reason": "not_found",
                      "message": "KB %s not found (404); nothing deleted" % kb_id}, 2)
    name = kb.get("name") or ""

    # 4. in-flight drain guard (kb_id only; scope-independent)
    try:
        inflight = kb_utils.kb_inflight(base, admin_key, kb_id)
    except Exception as e:
        return _emit({"action": "delete", "kb_id": kb_id, "name": name,
                      "status": "error", "reason": "guard_failed",
                      "message": str(e)}, 2)
    if inflight is None:
        return _emit({"action": "delete", "kb_id": kb_id, "name": name,
                      "status": "refused", "reason": "verify_unavailable",
                      "message": "could not verify in-flight state for KB %s (%s) "
                                 "via gateway /status; OWUI down or bad key -- delete "
                                 "would fail too; aborting" % (name, kb_id)}, 2)
    pending, processing = inflight
    if pending + processing > 0:
        return _emit({"action": "delete", "kb_id": kb_id, "name": name,
                      "status": "refused", "reason": "drain_in_flight",
                      "pending": pending, "processing": processing,
                      "message": "index drain in flight for KB %s (%s); "
                                 "pending=%d processing=%d; complete it first: "
                                 "make kb-index-finalize KB=%s, then retry"
                                 % (name, kb_id, pending, processing, name)}, 2)

    # 5. delete
    try:
        kb_utils.delete_kb(base, admin_key, kb_id)
    except Exception as e:
        return _emit({"action": "delete", "kb_id": kb_id, "name": name,
                      "status": "error", "reason": "delete_failed",
                      "message": str(e)}, 2)
    return _emit({"action": "delete", "kb_id": kb_id, "name": name,
                  "status": "deleted"}, 0)


if __name__ == "__main__":
    sys.exit(main())