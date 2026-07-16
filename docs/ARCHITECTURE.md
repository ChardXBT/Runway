# Architecture

The CLI, FastAPI routes, and Next.js UI call service-layer operations. Services own capture,
catalogue, analysis, retrieval, discovery, captions, proposals, publishing boundaries, and audit
events. Repository helpers own SQLite transactions. Provider and runtime protocols isolate all
external integration code.

```text
headed/manual/fixture inputs -> canonical catalogue + media -> profile/retrieval
                                                        -> discovery/ranking
                                                        -> captions/proposals
                                                        -> human approval
                                                        -> InternalPublisher only
```

All default paths are offline. The API listens only on `127.0.0.1` and serves media from the local
data directory.
