"""ONLY the REAL `/ws/activity` router, mounted for a real-uvicorn refusal test (SV-W4-12).

Why a separate app: the full `app.main` needs Postgres/Redis during lifespan startup, and the
question here is the HANDSHAKE — which runs before any of that.  Nothing under test is replaced or
re-implemented: the router, its dependencies and its refusal path are imported from the product
tree (`app/api/v1/ws.py`); the FastAPI instance around it is the only test scaffolding.

Run by `tests/unit/api/test_w5_ws_handshake_real.py` as
`python -m uvicorn tests.support.ws_refusal_app:app` in a subprocess.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.api.v1.ws import router as ws_router

app = FastAPI()
app.include_router(ws_router)
