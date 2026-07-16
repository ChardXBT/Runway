# API application

The ASGI implementation lives in the installable `leeway.api` package so the CLI, tests, and
Uvicorn use the same service contracts. `main.py` is a compatibility entry point.
