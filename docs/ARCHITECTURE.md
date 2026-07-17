# Architecture

The CLI, FastAPI routes, and Next.js UI call service-layer operations. Services own capture,
catalogue, analysis, retrieval, discovery, captions, proposals, publishing boundaries, and audit
events. Repository helpers own SQLite transactions. Provider and runtime protocols isolate all
external integration code.

```text
headed/browser-agent/manual/fixture inputs -> canonical catalogue + media -> profile/retrieval
                                                                      -> Codex image analysis
                                                                      -> discovery/ranking
                                                                      -> Codex captions/proposals
                                                                      -> feedback-aware reranker
                                                                      -> human approval/provenance
                                                                      -> InternalPublisher
                                                                      -> guarded YouTube publisher
```

The offline fixture path uses the mock runtime. The real model path uses the project-local Codex
CLI with ChatGPT authentication and no API fallback. Caption feedback is append-only and enters
bounded retrieval immediately. The external publisher is a separate service boundary: it cannot
be invoked by a runtime, requires explicit state transitions and one-time confirmation, and is
feature-gated off by default. The API listens only on `127.0.0.1` and serves media from the local
data directory.
