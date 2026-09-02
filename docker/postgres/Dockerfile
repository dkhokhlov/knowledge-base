# syntax=docker/dockerfile:1.9
#
# pgvector Postgres (pgvector/pgvector:pg16) + the ParadeDB `pg_search`
# extension, which provides real BM25 full-text search (IDF + length
# normalization) and replaces OWUI's `plainto_tsquery` + `ts_rank_cd` FTS arm
# (see docker/open-webui/PATCH.md patch 10). pgvector gives the vector index;
# pg_search gives the BM25 lexical arm of the hybrid retrieval.
#
# LICENSE: pg_search is AGPL-3.0. This image is LOCAL-ONLY -- it is never
# pushed to ghcr.io (pull_policy: never in compose.yml; no push target in the
# Makefile). The AGPL .deb is downloaded at build time from ParadeDB's GitHub
# release; it is NOT committed to this (MIT) repo (no redistribution). Rebuild:
#   docker compose build postgres

# shared_preload_libraries=pg_search is baked into the image via the wrapper
# entrypoint below (NOT a compose `command:` override) so it cannot be lost by
# an invocation that omits the override -- a bare `docker compose up postgres`,
# a DR restore, or the e2e project-targeting gap. pg_search refuses to load
# without the preload; because OWUI's hybrid_search swallows the resulting
# error (HTTP 200 + 0 hits), the make kb-check probe (scripts/kb_check.py) is
# the fail-loud detector for a missing preload.

ARG PGVECTOR_IMAGE_TAG=pg16
FROM pgvector/pgvector:${PGVECTOR_IMAGE_TAG}

ARG PG_SEARCH_VERSION
ARG PG_SEARCH_DEB_SHA256

# ADD fetches the .deb via the BuildKit builder's HTTP client (the base postgres
# image ships no curl/wget). Pinned by version (build arg) and verified by
# sha256sum (RUN). The URL follows GitHub's 302 to the CDN; ADD follows it.
ADD https://github.com/paradedb/paradedb/releases/download/v${PG_SEARCH_VERSION}/postgresql-16-pg-search_${PG_SEARCH_VERSION}-1PARADEDB-bookworm_amd64.deb /tmp/pg_search.deb

RUN echo "${PG_SEARCH_DEB_SHA256}  /tmp/pg_search.deb" | sha256sum -c - \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/pg_search.deb \
 && rm -f /tmp/pg_search.deb \
 && rm -rf /var/lib/apt/lists/*

# Wrap the original postgres entrypoint so shared_preload_libraries=pg_search
# is always set. With no compose `command:` (the kb-postgres case), "$@" is
# empty and the official entrypoint's `-`-prefix branch (docker-entrypoint.sh:
# `set -- postgres "$@"`) prepends `postgres`, yielding
#   postgres -c shared_preload_libraries=pg_search
# If a `command: postgres -c x=y` is ever added, "$@" carries it through
# unchanged (no duplicated `postgres` arg).
RUN printf '#!/bin/sh\nexec docker-entrypoint.sh "$@" -c shared_preload_libraries=pg_search\n' \
      > /usr/local/bin/pg-search-entrypoint.sh \
 && chmod +x /usr/local/bin/pg-search-entrypoint.sh

ENTRYPOINT ["pg-search-entrypoint.sh"]
CMD ["postgres"]