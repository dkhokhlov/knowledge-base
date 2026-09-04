# syntax=docker/dockerfile:1.9
#
# Thin overlay on the pinned graphiti-rest image that bakes in bootstrap.py
# (the Ollama-compatible client injection). The base image's default
# get_graphiti() builds ZepGraphiti with no llm_client, so the base default
# OpenAIClient (OpenAI Responses API) is used; Ollama cannot satisfy it and
# entity/fact extraction silently stores nothing. bootstrap.py overrides the
# FastAPI dependency to inject OpenAIGenericClient (Chat Completions +
# json_schema structured outputs, the >= 0.29 default) at temperature=0 and the
# nomic-embed-text embedder @ 768, so Ollama extraction works. There is no
# config switch for this; the injection is required. See docker/graphiti/bootstrap.py.
#
# No CMD/ENTRYPOINT: compose sets entrypoint ["/app/ollama-host.sh"] (the shared
# OLLAMA_HOST localhost->host.docker.internal shim, bind-mounted) + command
# ["python", "/app/bootstrap.py"]. The base image ENTRYPOINT is null, so the
# shim is safe to set; it execs "$@" = the command.
#
# Build:  docker compose build graphiti
# Base bump: change GRAPHITI_IMAGE_TAG in .env (rides the image: name -> forces
#           a rebuild). ARG has no default; an empty value fails loud
#           (invalid reference format) -- the compose-level :? guard fires first.
ARG GRAPHITI_IMAGE_TAG
FROM ghcr.io/dkhokhlov/graphiti-rest:${GRAPHITI_IMAGE_TAG}

# Base image is USER=app, WORKDIR=/app. COPY lands root:root 0644; python only
# needs read. No name collision: the bind mount occupied /app/bootstrap.py before
# the bake; COPY has the same overwrite semantics.
COPY bootstrap.py /app/bootstrap.py