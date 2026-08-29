# Fact memory (Graphiti)

The **temporal fact memory** half of the stack: [Graphiti](https://github.com/getzep/graphiti) over [Neo4j](https://neo4j.com/), reached through the api-gateway at `KB_HOST/memory/*` (internal REST server; no direct agent access). Extracted from the README intro.

## Temporal model

- Every **episode** is **time-stamped**.
- Extracted **facts** and **edges** are **time-bound**: a fact is true over a **time window**.
- A superseded fact is **invalidated, not deleted**.
- The graph therefore represents:
  - **current truth**,
  - **what was true when**,
  - **how knowledge changed**.
- This is beyond static vector retrieval of fixed text chunks: retrieval returns what a document says now; the graph also says what was true before and what changed.

## Motivation

Scattered, untrimmed README, notes, and tracker files across projects:

- every context load pays a growing **token tax**;
- specific facts become hard to find.

Fact memory replaces them with one **searchable temporal graph**:

- accumulated knowledge stays **findable**;
- it does not bloat linearly with every addition.

## Where next

- Commands + auth surface: [docs/agents.md](agents.md)
- Operations (env vars, Ollama host, troubleshooting): [docs/operations.md](operations.md)