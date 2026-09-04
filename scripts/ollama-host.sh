#!/bin/sh
# Entrypoint shim for the Ollama consumers that read the Ollama URL at runtime
# (graphiti, markitdown-ocr): rewrite localhost/127.0.0.1 -> host.docker.internal
# in the Ollama URL env vars, then exec the real command ("$@").
#
# Why: OLLAMA_HOST is Ollama's native client env var and `localhost` is the shell
# convention for a host-local Ollama. Inside a container, `localhost` is the
# container's own loopback (no Ollama there); compose adds
# `extra_hosts: host.docker.internal:host-gateway` so the Docker host is
# reachable as host.docker.internal. This shim bridges the two conventions, so
# an operator may set OLLAMA_HOST=http://localhost:11434 in the shell (where it
# works) and the containers still reach the host Ollama.
#
# Open WebUI does NOT use this shim: it persists rag.ollama.base_url to webui.db
# on first boot and uses the DB thereafter (it ignores the env). `make
# config-rag` (part of `make provision`, step 7) writes the translated value to
# that DB, and preflight.sh compares the DB against the translated OLLAMA_HOST.
# Both use the SAME regex as below, expressed in Python (scripts/config-rag.sh,
# scripts/preflight.sh). One rule, two languages: keep them in sync.
#
# Idempotent + non-matching: a non-localhost OLLAMA_HOST (e.g. a remote GPU host
# such as http://mini4:11434) is unchanged, so mounting this shim is always safe.

# Rewrite localhost|127.0.0.1 -> host.docker.internal in URL $1; print the
# result. Keeps scheme, port, and path. sed -E is portable (GNU + busybox).
_ollama_translate() {
  printf '%s\n' "$1" | sed -E 's#(https?://)(localhost|127\.0\.0\.1)([:/]|$)#\1host.docker.internal\3#'
}

for _v in OLLAMA_HOST OPENAI_BASE_URL OLLAMA_BASE_URL; do
  eval "_cur=\${$_v:-}"
  [ -n "$_cur" ] || continue
  _new=$(_ollama_translate "$_cur")
  [ "$_new" != "$_cur" ] || continue
  export "$_v=$_new"
done

exec "$@"