"""Keep a credential out of the server's ACCESS LOG, on every transport (W6-S3, wave 7).

THE GAP THIS CLOSES.  A URL is not a credential channel, and the access log is a file.  Wave 6
scrubbed the WebSocket scope only (`app/api/v1/ws.py::_redact_query_string`), so an HTTP request
that carried the owner key in the query string — the legacy `?api_key=<key>` form the frontend
never sends but a hand-rolled client still can — was written verbatim into the uvicorn access line
(measured in wave 6: `GET /api/v1/sites?api_key=… 401`, W6-S3).  A log line outlives the rotation it
described, so the durable answer is to keep the pair out of the log in the first place.

WHY THE ASGI SCOPE IS THE ONE PLACE THIS CAN BE DONE.  uvicorn formats its access line from the live
scope dict at response time — `get_path_with_query_string(self.scope)` in
`uvicorn/protocols/http/h11_impl.py` (the same call in `httptools_impl.py`), and the WebSocket
protocol formats the same scope at accept/close.  The scope this middleware receives is the very
object uvicorn logs from, so rewriting `scope["query_string"]` BEFORE the response starts removes
the credential from the line the server is about to emit, without touching a logger, a formatter or
the message of any existing record.  (`scope["raw_path"]` never carries the query — uvicorn builds
it with `event.target.partition(b"?")` — so `query_string` is the only field to scrub.)

WHAT IS NOT A CREDENTIAL.  `ticket` is deliberately NOT in the set: it is the supported URL form for
a browser socket, it is an HMAC over its own expiry stamp with a 30 s lifetime, and it cannot be
turned back into the key.  Scrubbing it would break the socket, not protect anything.

WHY THE WEBSOCKET HANDLER STILL CALLS THIS ITSELF.  The socket must READ `?key=` (to refuse it with
the specific "the API key must not travel in a URL" reason instead of the generic missing-credential
one) and `?ticket=` before the values disappear, so it captures them and then scrubs — synchronously,
before `accept()`, which is still before the line uvicorn emits.  An HTTP request never authenticates
from the query string (pinned by `tests/unit/api/test_api_key_auth.py`), so the middleware scrubs it
unconditionally, before routing.  One definition, two call sites, different timing needs.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Final

from starlette.types import ASGIApp, Receive, Scope, Send

#: Query parameters that are CREDENTIALS: never accepted as auth, and never allowed to survive into
#: a log line.  `ticket` is not here on purpose (see the module docstring).
CREDENTIAL_QUERY_KEYS: Final[frozenset[str]] = frozenset({"key", "api_key", "api-key"})


def scrub_query_credentials(scope: Scope) -> tuple[str, ...]:
    """Remove every credential-bearing pair from `scope["query_string"]`, IN PLACE.

    Returns the NAMES removed (never the values), in first-seen order and de-duplicated, so a
    caller can report "a credential arrived in the URL" without re-reading the credential.  Returns
    an empty tuple when nothing matched — the fast path, which leaves the raw bytes byte-identical
    (so no request's query encoding is rewritten merely because this middleware ran).

    Non-credential pairs are preserved and re-encoded with `urlencode`; a blank value is kept
    (`keep_blank_values=True`), because `?status=` and no `status` are different requests to the
    routes that declare one.  Re-encoding is the only lossy step and it only runs on a request that
    carried a credential — a request that was going to be refused anyway.
    """
    raw = scope.get("query_string") or b""
    if not raw:
        return ()
    pairs = urllib.parse.parse_qsl(raw.decode("latin-1"), keep_blank_values=True)
    kept = [(name, value) for name, value in pairs if name not in CREDENTIAL_QUERY_KEYS]
    if len(kept) == len(pairs):
        return ()
    removed: list[str] = []
    for name, _value in pairs:
        if name in CREDENTIAL_QUERY_KEYS and name not in removed:
            removed.append(name)
    scope["query_string"] = urllib.parse.urlencode(kept).encode("latin-1")
    return tuple(removed)


class RequestLogScrub:
    """ASGI middleware: no credential-bearing query pair reaches uvicorn's access line.

    Installed in `app.main.create_app` INSIDE `CORSMiddleware` and OUTSIDE `ErrorBoundary`, so it
    runs before any route, dependency or exception handler — including for the 401 this request is
    about to earn — and the access line uvicorn writes afterwards reads the scrubbed scope.

    HTTP only.  A WebSocket handshake is scrubbed by its own handler, which needs the raw values
    first (see the module docstring).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            scrub_query_credentials(scope)
        await self.app(scope, receive, send)


def scrubbed_log_target(scope: dict[str, Any]) -> str:
    """The path+query string uvicorn would log for this scope — for tests and probes.

    Deliberately mirrors `uvicorn.protocols.utils.get_path_with_query_string` (quote the path, then
    `?` + the scope's query bytes) so a test can assert what the SERVER would write without
    importing the server's own module into product code.
    """
    target = urllib.parse.quote(scope.get("path") or "")
    raw = scope.get("query_string") or b""
    if raw:
        target = f"{target}?{raw.decode('ascii', 'replace')}"
    return target


__all__ = ["CREDENTIAL_QUERY_KEYS", "RequestLogScrub", "scrub_query_credentials", "scrubbed_log_target"]
