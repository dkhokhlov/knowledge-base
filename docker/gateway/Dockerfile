# api-gateway: zero-dependency Python stdlib service (no pip install).
# Authorizes KB_API_KEY via Open WebUI, bridges to Graphiti REST, discovers
# groups from Neo4j, and provisions admin-created users. Runs non-root.
FROM python:3.12-slim

# A system user to run the gateway. uid/gid chosen not to collide with the
# host owner. No data mounts (stateless; config via env only).
RUN addgroup --system --gid 2000 kbg && \
    adduser --system --uid 2000 --ingroup kbg --no-create-home kbg

WORKDIR /app
# Build context is the repo root (compose.yml) so the image can reach the shared
# matcher `scripts/kb_ignore.py` (the gateway imports it; kb.py clones it inline;
# gdrive-sync calls its CLI). The repo-root .dockerignore excludes everything
# else -> the context stays tiny (gateway/ + scripts/kb_ignore.py).
COPY docker/gateway/app.py docker/gateway/neo4j.py docker/gateway/owui.py docker/gateway/graphiti.py docker/gateway/authorize.py scripts/kb_ignore.py ./

USER kbg
EXPOSE 8010

# python:3-slim has no curl/wget — use the stdlib for the healthcheck.
HEALTHCHECK --interval=15s --timeout=5s --retries=10 --start-period=20s \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8010/health',timeout=3).status==200 else 1)"

CMD ["python", "app.py"]