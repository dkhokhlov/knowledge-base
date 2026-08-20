"""Bootstrap entrypoint for the zepai/graphiti REST server.

Mounted into the container at /app/bootstrap.py and run as the command
(`python /app/bootstrap.py`). It builds a fresh FastAPI app (own lifespan +
shutdown close) that re-uses the image's routers, but overrides the Graphiti
client dependency so extraction uses clients compatible with Ollama.

Why this exists (the memory stack was non-functional with Ollama):
  - The image's default `get_graphiti()` builds `ZepGraphiti` with no
    `llm_client`, so Graphiti's base default `OpenAIClient()` is used. That
    client targets the OpenAI Responses API; Ollama answers /v1/responses
    with a shape Graphiti cannot parse, so entity/fact extraction silently
    stores nothing. The package METADATA says: use OpenAIGenericClient for
    Ollama (Chat Completions + json_object). There is no config switch for
    this, so we inject it here.
  - The default embedder is OpenAIEmbedder() with model text-embedding-3-small
    and dim 1024, neither of which exists on this stack (we use
    nomic-embed-text @ 768). Injecting the embedder also fixes the vector
    index dimension, which the startup client creates via
    build_indices_and_constraints.

Lifecycle (codex review):
  - The stock `get_graphiti` is an async generator that `await client.close()`
    in a `finally` (per-request close). The /messages worker runs AFTER the
    request returns 202, so a per-request-closed driver races the worker. We
    instead hold a process-level SINGLETON, return it from the dependency
    override (no per-request close), and close it only on application shutdown.
  - `Depends(get_graphiti)` captures the function object at router import, so
    reassigning a module attribute does not propagate; we use
    `app.dependency_overrides[get_graphiti]`, the documented override path.
"""
import asyncio
import json
import logging
import os
import traceback
from contextlib import asynccontextmanager

import openai
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.graphiti import Graphiti
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize
from graphiti_core.llm_client.errors import RateLimitError
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graph_service.config import get_settings
from graph_service.routers import ingest, retrieve
from graph_service.routers.ingest import async_worker
from graph_service.zep_graphiti import ZepGraphiti, get_graphiti

logger = logging.getLogger(__name__)

# Process-level Graphiti client. Built once at startup, returned to every
# request via the dependency override, closed once at shutdown.
_SINGLETON = None


class _SchemaEnforcingClient(OpenAIGenericClient):
    """OpenAIGenericClient that uses Ollama structured outputs (json_schema).

    Why this subclass exists (the json_object default breaks extraction):
      OpenAIGenericClient._generate_response requests
      ``response_format={'type': 'json_object'}`` and relies on the schema being
      appended to the prompt as TEXT (in generate_response). With a local
      Ollama model that is only a 14B non-reasoning model, the model frequently
      echoes the schema object back as its answer (e.g. the EntityAttributes
      step returns ``{"properties":..., "required":..., "title":..., "type":...,
      "summary": {schema}}``) instead of filling it in. The ``summary`` value is
      then a MAP, which Neo4j rejects ("Property values can only be of primitive
      types"), so add_episode aborts before any fact is stored.

      Ollama >= 0.32 supports OpenAI-compatible structured outputs
      (``response_format={'type':'json_schema','json_schema':{...}}``), which
      enforces the schema server-side so the model cannot echo it. This override
      passes the response_model's JSON schema through that path; the rest of the
      call (message cleaning, parsing, retry in generate_response) is unchanged.

      A reasoning model (e.g. gemma4:12b) is still unsuitable regardless of
      mode: its thinking chain can exhaust max_tokens before any content is
      emitted (finish_reason=length -> empty content -> json.loads('') fails).
      MODEL_NAME must therefore be a non-reasoning model; see .env.
    """

    async def _generate_response(
        self,
        messages,
        response_model=None,
        max_tokens=DEFAULT_MAX_TOKENS,
        model_size=ModelSize.medium,
    ):
        openai_messages = []
        for m in messages:
            m.content = self._clean_input(m.content)
            if m.role == 'user':
                openai_messages.append({'role': 'user', 'content': m.content})
            elif m.role == 'system':
                openai_messages.append({'role': 'system', 'content': m.content})
        # When a response_model is given, enforce it server-side via json_schema
        # (Ollama structured outputs). Otherwise fall back to json_object.
        if response_model is not None:
            response_format = {
                'type': 'json_schema',
                'json_schema': {
                    'name': response_model.__name__,
                    'schema': response_model.model_json_schema(),
                    'strict': True,
                },
            }
        else:
            response_format = {'type': 'json_object'}
        try:
            response = await self.client.chat.completions.create(
                model=self.model or 'gpt-4.1-mini',
                messages=openai_messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format=response_format,
            )
            result = response.choices[0].message.content or ''
            return json.loads(result)
        except openai.RateLimitError as e:
            raise RateLimitError from e
        except Exception as e:
            logger.error('Error in generating LLM response: %s', e)
            raise


class _InjectedZepGraphiti(ZepGraphiti):
    """ZepGraphiti with both llm_client and embedder injected.

    ZepGraphiti.__init__ only accepts llm_client (not embedder); calling it
    would leave self.embedder / self.clients.embedder at the OpenAI defaults.
    Bypass it and call Graphiti.__init__ directly, which builds self.clients
    from both kwargs so the embedder is consistent everywhere it is read.
    """

    def __init__(self, uri, user, password, llm_client, embedder):
        Graphiti.__init__(self, uri=uri, user=user, password=password,
                          llm_client=llm_client, embedder=embedder)


def _build_client(settings):
    """Construct the Ollama-compatible Graphiti client from env settings."""
    # EMBEDDER_DIMENSIONS is not a REST Settings field; read it from the env
    # the compose service sets. Default 768 (nomic-embed-text). The vector
    # index is created at this dim, so a wrong value makes 768-dim writes fail.
    dim = int(os.environ.get("EMBEDDER_DIMENSIONS", "768"))
    llm = _SchemaEnforcingClient(config=LLMConfig(
        api_key=settings.openai_api_key,
        model=settings.model_name,
        small_model=settings.model_name,  # no separate small model on this stack
        base_url=settings.openai_base_url,
    ))
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(
        embedding_model=settings.embedding_model_name,
        embedding_dim=dim,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    ))
    return _InjectedZepGraphiti(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password, llm, embedder)


async def _worker_loop():
    """Drain the /messages async queue with logging + error isolation.

    The image's AsyncWorker.worker() runs `await job()` inside a
    `try/except CancelledError`; any OTHER exception from the job (an
    extraction failure) propagates, kills the worker task, and is not logged
    prominently — so /messages silently stops processing after one bad
    episode. This loop catches and logs every job failure, then keeps
    draining, so one bad episode cannot wedge the queue. It reads the same
    `async_worker.queue` the /messages handler puts to.
    """
    q = async_worker.queue
    while True:
        try:
            job = await q.get()
            await job()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print("bootstrap: /messages job failed (continuing): %s\n%s" % (
                e, traceback.format_exc()), flush=True)


_worker_task = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _SINGLETON, _worker_task
    s = get_settings()
    _SINGLETON = _build_client(s)
    # build_indices_and_constraints creates the vector index at the embedder's
    # dim; doing it here (not per request) sets 768 once at startup.
    await _SINGLETON.build_indices_and_constraints()
    # Replace the router's own worker (it dies silently on the first job that
    # raises) with our robust loop over the same queue. The router's lifespan
    # may have started a task already; cancel it so exactly one worker drains
    # the queue.
    if async_worker.task is not None:
        async_worker.task.cancel()
        try:
            await async_worker.task
        except Exception:
            pass
        async_worker.task = None
    _worker_task = asyncio.create_task(_worker_loop())
    yield
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except Exception:
            pass
    if _SINGLETON is not None:
        await _SINGLETON.close()
    _SINGLETON = None


async def _get_graphiti_override():
    # Return the startup-built singleton; do NOT close per request (the async
    # /messages worker outlives the request that queued it).
    return _SINGLETON


app = FastAPI(lifespan=lifespan)
app.include_router(retrieve.router)
app.include_router(ingest.router)


@app.get("/healthcheck")
async def healthcheck():
    return JSONResponse(content={"status": "healthy"}, status_code=200)


# Override the Graphiti client dependency the routers declare via
# `Annotated[ZepGraphiti, Depends(get_graphiti)]`. Keyed on the exact function
# object the routers captured, so the override propagates to every endpoint.
app.dependency_overrides[get_graphiti] = _get_graphiti_override


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)