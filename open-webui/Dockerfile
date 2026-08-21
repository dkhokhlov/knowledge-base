# syntax=docker/dockerfile:1.9
#
# Thin overlay on the pinned official Open WebUI image that applies two build-time
# patches to the backend: the path-aware dedup-hash patch (retrieval.py) and the
# upload-idempotency patch (files.py). See open-webui/PATCH.md for the why, the
# anchors, and the rebase steps.
#
# No frontend rebuild: the patches touch two backend router files in place.
# Build seconds, not minutes. Each apply script fails loud (exit 1) if its anchor
# has drifted on a base-digest bump, so a silent mismatch cannot ship.
#
# Build:  docker compose build openwebui
# Rebuild after a base bump: change OPENWEBUI_BASE_DIGEST below (or via
#         --build-arg), rebuild. If an anchor drifted, the build fails and
#         PATCH.md's rebase steps apply.

ARG OPENWEBUI_BASE_DIGEST=sha256:6a773e5c3a246b65cbe74ce942b294292c0e5f81c138f703d111bc162f7d7c3d
FROM ghcr.io/open-webui/open-webui:main@${OPENWEBUI_BASE_DIGEST}

COPY apply_path_hash.py /tmp/apply_path_hash.py
COPY apply_upload_idempotency.py /tmp/apply_upload_idempotency.py
RUN python3 /tmp/apply_path_hash.py && python3 /tmp/apply_upload_idempotency.py && rm /tmp/apply_path_hash.py /tmp/apply_upload_idempotency.py