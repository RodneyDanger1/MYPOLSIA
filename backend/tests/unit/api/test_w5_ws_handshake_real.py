"""SV-W4-12 (wave 5) — the socket refusal codes on the REAL WIRE, against a real uvicorn.

Wave-4's socket tests asserted `close(code=1008)` on a hand-written fake, and `TestClient`'s
`WebSocketDisconnect.code`.  Both model the ASGI message, never the wire: uvicorn answers a
`websocket.close` sent BEFORE `accept()` with **HTTP 403** and discards the code
(`uvicorn/protocols/websockets/websockets_impl.py:287-295`, read in this venv), and a browser
cannot read a failed handshake's status — its `CloseEvent.code` is **1006**, identical to a dead
network.  The live image measured exactly that (wave-5 probe): 9 refusals, all HTTP 403, zero close
codes reaching the client.

So this module starts a REAL uvicorn in a subprocess serving the REAL `app/api/v1/ws.py` router
(`tests/support/ws_refusal_app.py`) and measures two independent client points of view:

  * a RAW TCP socket that sends the HTTP upgrade by hand and decodes the FIRST WebSocket frame —
    byte for byte what a browser's WebSocket implementation parses;
  * a real `websockets` client — the closest thing to a browser without a browser.

RED leg (the pre-fix handler: close before accept): the raw status is `HTTP/1.1 403`, no close
frame exists, and the `websockets` client raises `InvalidStatus(403)` — never a close code.

The subprocess gets a CLEAN environment (AGENTS.md host rules: the harness `PYTHONPATH` otherwise
wins and the child cannot import the app), and every server is terminated in a `finally`.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import websockets

pytestmark = pytest.mark.asyncio

BACKEND_DIR = Path(__file__).resolve().parents[3]
WS_APP = "tests.support.ws_refusal_app:app"
OWNER_KEY = "w5-real-socket-owner-key"
START_TIMEOUT_SECONDS = 40.0


# ── the server under test ─────────────────────────────────────────────────────────────────────


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    """A child environment with only the variables a Python app needs (AGENTS.md host rules)."""
    env = {
        name: os.environ[name]
        for name in (
            "PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE",
            "LOCALAPPDATA", "APPDATA", "PATHEXT", "COMSPEC",
        )
        if name in os.environ
    }
    env.update(
        {
            "PYTHONPATH": str(BACKEND_DIR),
            "PYTHONIOENCODING": "utf-8",
            # The socket tests must not depend on a model runtime, a sandbox flag, or a DB:
            # the handshake refusal happens before any of them.
            "CLAUDE_CLI_MOCK": "true",
            "SANDBOX_MODE": "true",
            "API_KEY": OWNER_KEY,
        }
    )
    env.update(extra)
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _uvicorn(extra_env: dict[str, str]) -> Iterator[int]:
    """Start the real app on a free loopback port; yield the port; always stop the server."""
    port = _free_port()
    log_path = Path(tempfile.mkdtemp(prefix="w5-uvicorn-")) / f"server-{port}.log"
    argv = [
        sys.executable, "-m", "uvicorn", WS_APP,
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
    ]
    with open(log_path, "w", encoding="utf-8") as stream:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, test-owned server
            argv, cwd=str(BACKEND_DIR), env=_clean_env(extra_env),
            stdout=stream, stderr=subprocess.STDOUT,
        )
    try:
        _wait_until_serving(port, proc, log_path)
        yield port
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server must not hang CI
            proc.kill()
            proc.wait(timeout=10)
        # Windows may still hold the child's inherited handle for a moment after it exits; a
        # leftover temp log is harmless, a teardown error is not.
        with contextlib.suppress(OSError):
            log_path.unlink(missing_ok=True)


def _wait_until_serving(port: int, proc: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"uvicorn exited before serving (code {proc.returncode}):\n"
                f"{log.read_text(encoding='utf-8', errors='replace')[-3000:]}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(
        f"uvicorn did not serve on 127.0.0.1:{port} within {START_TIMEOUT_SECONDS}s:\n"
        f"{log.read_text(encoding='utf-8', errors='replace')[-3000:]}"
    )


@pytest.fixture(scope="module")
def refusal_server() -> Iterator[int]:
    with _uvicorn({}) as port:
        yield port


@pytest.fixture(scope="module")
def capacity_server() -> Iterator[int]:
    """A server whose per-session cap is ZERO: every authenticated handshake hits the cap."""
    with _uvicorn({"WS_MAX_SOCKETS_PER_CLIENT": "0"}) as port:
        yield port


# ── the two client points of view ────────────────────────────────────────────────────────────


def _raw_handshake(
    port: int, path: str, headers: dict[str, str] | None = None, *, timeout: float = 10.0
) -> dict[str, Any]:
    """Send the HTTP upgrade by hand and decode the FIRST WebSocket frame the server sends."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request)
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        status = int(status_line.split(" ")[1]) if " " in status_line else None
        result: dict[str, Any] = {
            "status": status,
            "status_line": status_line,
            "first_frame": None,
        }
        if status != 101:
            result["body"] = rest[:200].decode("latin-1", "replace")
            return result

        sock.settimeout(timeout)
        payload = rest
        while len(payload) < 2:
            chunk = sock.recv(4096)
            if not chunk:
                break
            payload += chunk
        first, second = payload[0], payload[1]
        opcode = first & 0x0F
        length = second & 0x7F
        offset = 2
        if length == 126:
            while len(payload) < offset + 2:
                payload += sock.recv(4096)
            (length,) = struct.unpack("!H", payload[offset:offset + 2])
            offset += 2
        elif length == 127:
            while len(payload) < offset + 8:
                payload += sock.recv(4096)
            (length,) = struct.unpack("!Q", payload[offset:offset + 8])
            offset += 8
        while len(payload) < offset + length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            payload += chunk
        data = payload[offset:offset + length]
        frame: dict[str, Any] = {"opcode": opcode, "length": length}
        if opcode == 8 and len(data) >= 2:
            frame["close_code"] = struct.unpack("!H", data[:2])[0]
            frame["close_reason"] = data[2:].decode("utf-8", "replace")
        result["first_frame"] = frame
        return result


async def _client_view(
    url: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0
) -> dict[str, Any]:
    """What a browser-equivalent client reports for this handshake."""
    try:
        async with websockets.connect(
            url, open_timeout=timeout, additional_headers=headers
        ) as socket:
            try:
                await socket.recv()
                return {"accepted": True, "close_code": None, "frame": "data"}
            except websockets.ConnectionClosed as exc:
                # `exc.code` is deprecated in websockets>=13.1: the received close frame is the
                # authoritative one (a browser's CloseEvent comes from exactly that frame).
                rcvd = getattr(exc, "rcvd", None)
                return {
                    "accepted": True,
                    "close_code": getattr(rcvd, "code", None),
                    "reason": str(getattr(rcvd, "reason", "") or ""),
                    "exception": type(exc).__name__,
                }
    except websockets.InvalidStatus as exc:
        return {"accepted": False, "handshake_http_status": exc.response.status_code}
    except websockets.ConnectionClosed as exc:  # pragma: no cover - legacy client shapes
        return {"accepted": False, "ws_close_code": exc.code}


# ── the RED/GREEN pins ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("case", "path", "headers"),
    [
        ("no credential", "/ws/activity", None),
        ("?key=<owner key> in the URL", f"/ws/activity?key={OWNER_KEY}", None),
        ("a forged ticket", "/ws/activity?ticket=v2.9999999999.subj.deadbeef", None),
    ],
)
async def test_a_refused_credential_is_a_close_frame_1008_not_an_http_403(
    refusal_server: int, case: str, path: str, headers: dict[str, str] | None
) -> None:
    """THE SV-W4-12 pin: the refusal must be OBSERVABLE to the client as 1008.

    OLD observable (wave-4 image): `HTTP/1.1 403`, `first_frame` None, browser `onclose` 1006.
    NEW observable: `HTTP/1.1 101`, then a single close frame carrying 1008 — and NOTHING else.
    """
    raw = _raw_handshake(refusal_server, path, headers)
    assert raw["status"] == 101, (
        f"{case}: the handshake was refused with HTTP {raw['status']} — a browser reads that as "
        f"close 1006 and cannot tell it from a dead network: {raw}"
    )
    frame = raw["first_frame"]
    assert frame is not None, f"{case}: a 101 handshake with no frame at all"
    assert frame["opcode"] == 8, (
        f"{case}: the FIRST frame is opcode {frame['opcode']} (a data frame), not a close — a "
        f"refused socket must never deliver data: {raw}"
    )
    assert frame["close_code"] == 1008, f"{case}: expected close 1008, got {frame}"
    assert "refused" in frame["close_reason"], (
        f"{case}: the close frame must name the refusal for the UI: {frame['close_reason']!r}"
    )

    view = await _client_view(
        f"ws://127.0.0.1:{refusal_server}{path}", timeout=10.0
    )
    assert view.get("accepted") is True, (
        f"{case}: the browser-equivalent client never completed the handshake, so it cannot read "
        f"the code: {view}"
    )
    assert view.get("close_code") == 1008, f"{case}: client saw {view}"


async def test_the_capacity_refusal_is_a_close_frame_1013(capacity_server: int) -> None:
    """1013 (try again later) must reach the client too, not just the log line."""
    raw = _raw_handshake(
        capacity_server, "/ws/activity", {"X-API-Key": OWNER_KEY}
    )
    assert raw["status"] == 101, (
        f"the capacity refusal answered HTTP {raw['status']} instead of a close code: {raw}"
    )
    frame = raw["first_frame"] or {}
    assert frame.get("opcode") == 8, f"expected a close frame, got {raw}"
    assert frame.get("close_code") == 1013, f"expected close 1013, got {frame}"
    assert "maximum" in frame.get("close_reason", ""), frame

    view = await _client_view(
        f"ws://127.0.0.1:{capacity_server}/ws/activity",
        headers={"X-API-Key": OWNER_KEY},
    )
    assert view.get("accepted") is True, f"the client never completed the handshake: {view}"
    assert view.get("close_code") == 1013, view


async def test_the_refusal_frame_reason_never_carries_the_credential(refusal_server: int) -> None:
    """A refusal is reported to the client; it must not echo what was presented."""
    raw = _raw_handshake(refusal_server, f"/ws/activity?key={OWNER_KEY}")
    frame = raw["first_frame"] or {}
    assert frame.get("close_code") == 1008, frame
    reason = frame.get("close_reason", "")
    assert OWNER_KEY not in reason, f"the close reason echoed the key: {reason!r}"
    assert json.dumps(frame).find(OWNER_KEY) == -1, frame
