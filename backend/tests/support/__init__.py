"""Test support that is NOT a test: shared harnesses for the wave-5 socket work.

`ws_handshake` reads a handshake through Starlette's `TestClient` while recording the ASGI frames
the route actually sends; `ws_refusal_app` is the minimal real app the uvicorn-subprocess test
serves.  Both are outside `app/`, so they never ship.
"""
