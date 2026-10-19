"""WAVE V19 / lane L3 — the HOST COMPANION SEND BRIDGE + the service's bridge transport.

RED-FIRST: written before `scripts/phone_send_bridge.py` and before the bridge branch in
`services/phone_send_service.py`.  At HEAD the bridge file does not exist (`_bridge` fails by name)
and every service pin here reaches the direct driver path instead of the configured bridge.

WHAT IS REAL HERE
    The bridge is started as its REAL `ThreadingHTTPServer` on loopback and every request travels a
    real socket.  The ONLY stub is `_run_driver` inside the bridge — the driver-subprocess boundary
    the brief names as stubbable in tests.  Service pins talk to that same real bridge, so the
    urllib client, the HTTP status mapping, the token header and the refusal envelopes are all
    exercised end to end.  Nothing in product code is mocked and no SMS can be sent: the stub
    driver never spawns a process.

FROZEN INTERFACE (wave-v19/V19-PLAN.md)
    bridge  POST /send  {"to", "body"} + X-Bridge-Token -> {status:"sent", sent_at, to_masked, driver}
                                                          | {status:"refused", code, message}
            GET  /health -> {status:"ok", paired, version}
    service PHONE_SEND_BRIDGE_URL set -> bridge transport (token required, E_CONFIG if absent);
            unset -> the existing direct driver path, unchanged.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import logging
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from app.core.errors import (
    E_AUTH,
    E_CONFIG,
    E_PHONE_UNAVAILABLE,
    E_SANDBOX_BLOCKED,
    E_VALIDATION,
    PolsiaError,
)
from app.services import phone_send_service as service

E_PHONE_SEND_UNPAIRED = service.E_PHONE_SEND_UNPAIRED

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_FILE = REPO_ROOT / "scripts" / "phone_send_bridge.py"
TOKEN = "bridge-token-0123456789abcdef"  # 32 chars
OTHER_TOKEN = "other-token-fedcba9876543210x"  # 32 chars
BODY = "Bridge canary — see you at the café."
SENT_PAYLOAD = {
    "ok": True,
    "data": {
        "sent_at": "2026-10-18T20:15:00-05:00",
        "to_masked": "••••4477",
        "driver": "cdp-google-messages",
    },
}
UNPAIRED_PAYLOAD = {
    "ok": False,
    "error": {
        "code": "E_PHONE_SEND_UNPAIRED",
        "message": "the automation Chrome is not paired with the phone",
        "how_to_fix": "pair Google Messages web once, then retry",
        "context": {},
    },
}

_BRIDGE_MODULE = None


def _bridge():
    """Import the bridge by path (scripts/ is not a package); RED leg fails HERE, by name."""
    global _BRIDGE_MODULE
    if _BRIDGE_MODULE is None:
        assert BRIDGE_FILE.is_file(), f"the bridge is missing: {BRIDGE_FILE}"
        spec = importlib.util.spec_from_file_location("phone_send_bridge_v19", BRIDGE_FILE)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("phone_send_bridge_v19", module)
        spec.loader.exec_module(module)
        _BRIDGE_MODULE = module
    return _BRIDGE_MODULE


# ── real HTTP helpers (http.client: no proxy env, exact headers) ─────────────────────────────


def _http(base: str, method: str, path: str, *, body: bytes | None = None,
          headers: dict[str, str] | None = None, timeout: float = 10.0):
    parsed = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), (json.loads(raw) if raw else None)
    finally:
        conn.close()


def _post(base: str, payload, token: str | None = TOKEN):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Bridge-Token"] = token
    return _http(base, "POST", "/send", body=json.dumps(payload).encode(), headers=headers)


def _stub_driver(monkeypatch, mod, payload, returncode=0):
    monkeypatch.setattr(mod, "_run_driver", lambda argv, timeout: mod.DriverRun(returncode, json.dumps(payload), ""))


class _CapturingDriver:
    """The one stubbed boundary: reads argv (and the private body file) instead of spawning."""

    def __init__(self, mod, payload, returncode=0):
        self.mod, self.payload, self.returncode = mod, payload, returncode
        self.argv: list[str] = []
        self.body: str | None = None
        self.timeout: float | None = None
        self.calls = 0

    def __call__(self, argv, timeout):
        self.calls += 1
        self.argv = list(argv)
        self.timeout = timeout
        if "--body-file" in argv:
            self.body = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
        return self.mod.DriverRun(self.returncode, json.dumps(self.payload), "")


@pytest.fixture
def bridge(monkeypatch):
    mod = _bridge()
    driver = _CapturingDriver(mod, SENT_PAYLOAD)
    monkeypatch.setattr(mod, "_run_driver", driver)
    server = mod.build_server("127.0.0.1", 0, token=TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield mod, f"http://127.0.0.1:{server.server_address[1]}", driver, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _StallHandler(socketserver.BaseRequestHandler):
    def handle(self):  # pragma: no cover - sleeps, then the client is gone
        time.sleep(2.0)


class _StallServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── bridge: configuration posture ───────────────────────────────────────────────────────────


def test_bridge_defaults_are_loopback_and_9227():
    mod = _bridge()
    assert mod.DEFAULT_HOST == "127.0.0.1"
    assert mod.DEFAULT_PORT == 9227
    options = mod.parse_args([])
    assert (options.host, options.port) == ("127.0.0.1", 9227)
    assert isinstance(mod.VERSION, str) and mod.VERSION
    assert mod.MAX_BODY_BYTES == 8192
    assert mod.REQUEST_TIMEOUT_S <= 90.0


def test_startup_refuses_without_token(monkeypatch, caplog):
    mod = _bridge()
    monkeypatch.delenv(mod.TOKEN_ENV, raising=False)
    with caplog.at_level(logging.DEBUG):
        assert mod.main(["--port", "9227"]) == 2
    assert mod.TOKEN_ENV in caplog.text
    assert "24" in caplog.text


def test_startup_refuses_short_token(monkeypatch, caplog):
    mod = _bridge()
    monkeypatch.setenv(mod.TOKEN_ENV, "too-short")
    with caplog.at_level(logging.DEBUG):
        assert mod.main(["--port", "9227"]) == 2
    assert mod.TOKEN_ENV in caplog.text


def test_token_is_env_only_never_an_argument():
    mod = _bridge()
    with pytest.raises(mod.BridgeConfigError):
        mod.parse_args(["--token", TOKEN])
    source = BRIDGE_FILE.read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source
    assert "argv" not in mod.TOKEN_ENV.lower()


def test_startup_banner_warns_on_non_loopback_and_never_carries_token():
    mod = _bridge()
    assert mod.is_loopback("127.0.0.1") is True
    assert mod.is_loopback("localhost") is True
    assert mod.is_loopback("0.0.0.0") is False
    assert mod.is_loopback("host.docker.internal") is False
    exposed = "\n".join(mod.startup_lines(mod.parse_args(["--host", "0.0.0.0"]), TOKEN))
    assert "firewall" in exposed.lower() and "token" in exposed.lower()
    assert TOKEN not in exposed
    loopback = "\n".join(mod.startup_lines(mod.parse_args([]), TOKEN))
    assert "firewall" not in loopback.lower()
    assert TOKEN not in loopback


# ── bridge: health ─────────────────────────────────────────────────────────────────────────


def test_health_reports_paired_from_real_check_argv(bridge):
    mod, base, driver, _server = bridge
    driver.payload = {"ok": True, "data": {"paired": True, "source": "cdp"}}
    status, _headers, payload = _http(base, "GET", "/health")
    assert status == 200
    assert payload["status"] == "ok" and payload["paired"] is True
    assert payload["version"] == mod.VERSION
    assert "--check" in driver.argv and "--send" not in driver.argv


def test_health_says_not_paired_when_driver_refuses(bridge, monkeypatch):
    mod, base, _driver, _server = bridge
    _stub_driver(monkeypatch, mod, {"ok": False, "error": {"code": "E_PHONE_UNAVAILABLE", "message": "no chrome"}}, 4)
    status, _headers, payload = _http(base, "GET", "/health")
    assert status == 200 and payload["status"] == "ok" and payload["paired"] is False


# ── bridge: auth + protocol ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("token", [None, "", "wrong-token-0123456789abcdef"])
def test_send_requires_the_exact_token(bridge, token):
    _mod, base, driver, _server = bridge
    status, _headers, payload = _post(base, {"to": "15551234567", "body": BODY}, token)
    assert status == 401
    assert payload["status"] == "refused" and payload["code"] == E_AUTH
    assert driver.calls == 0


def test_send_passes_driver_result_through(bridge):
    mod, base, driver, _server = bridge
    status, _headers, payload = _post(base, {"to": "15551234567", "body": BODY})
    assert status == 200
    assert payload == {
        "status": "sent",
        "sent_at": SENT_PAYLOAD["data"]["sent_at"],
        "to_masked": "••••4477",
        "driver": "cdp-google-messages",
    }
    assert "--send" in driver.argv and "15551234567" in driver.argv
    assert driver.body == BODY
    assert driver.timeout is not None and driver.timeout <= 90.0


def test_driver_refusal_is_passed_through_by_code(bridge, monkeypatch):
    mod, base, _driver, _server = bridge
    _stub_driver(monkeypatch, mod, UNPAIRED_PAYLOAD, 3)
    status, _headers, payload = _post(base, {"to": "15551234567", "body": BODY})
    assert status == 409
    assert payload["status"] == "refused"
    assert payload["code"] == E_PHONE_SEND_UNPAIRED
    assert "paired" in payload["message"].lower()


def test_unknown_driver_code_becomes_unavailable(bridge, monkeypatch):
    mod, base, _driver, _server = bridge
    _stub_driver(monkeypatch, mod, {"ok": False, "error": {"code": "E_NOT_A_CODE", "message": "x"}}, 1)
    status, _headers, payload = _post(base, {"to": "15551234567", "body": BODY})
    assert status == 503 and payload["code"] == E_PHONE_UNAVAILABLE


def test_other_methods_on_send_are_405(bridge):
    _mod, base, driver, _server = bridge
    status, headers, payload = _http(base, "GET", "/send", headers={"X-Bridge-Token": TOKEN})
    assert status == 405
    assert "POST" in headers.get("Allow", "")
    assert payload["status"] == "refused"
    assert driver.calls == 0


def test_body_cap_is_refused_before_the_driver(bridge):
    _mod, base, driver, _server = bridge
    status, _headers, payload = _http(
        base, "POST", "/send", body=b"x" * 9000,
        headers={"X-Bridge-Token": TOKEN, "Content-Type": "application/json"},
    )
    assert status == 413
    assert payload["status"] == "refused" and payload["code"] == E_VALIDATION
    assert driver.calls == 0


def test_malformed_json_and_missing_fields_are_named(bridge):
    _mod, base, driver, _server = bridge
    status, _headers, payload = _http(
        base, "POST", "/send", body=b"{not json",
        headers={"X-Bridge-Token": TOKEN, "Content-Type": "application/json"},
    )
    assert status == 400 and payload["code"] == E_VALIDATION
    status, _headers, payload = _post(base, {"to": "", "body": ""})
    assert status == 422 and payload["code"] == E_VALIDATION
    assert driver.calls == 0


def test_driver_timeout_is_refused_by_name(tmp_path, monkeypatch):
    mod = _bridge()
    slow = tmp_path / "slow_driver.py"
    slow.write_text("import time\ntime.sleep(3)\n", encoding="utf-8")
    monkeypatch.setattr(mod, "REQUEST_TIMEOUT_S", 0.25)
    server = mod.build_server("127.0.0.1", 0, token=TOKEN, driver=slow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        started = time.monotonic()
        status, _headers, payload = _post(base, {"to": "15551234567", "body": BODY})
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert status == 503
    assert payload["status"] == "refused" and payload["code"] == E_PHONE_UNAVAILABLE
    assert "timed out" in payload["message"].lower()
    assert elapsed < 2.5, f"the bridge did not enforce its own timeout ({elapsed:.2f}s)"


def test_bridge_never_logs_body_or_token(bridge, caplog):
    _mod, base, driver, _server = bridge
    canary = "CANARY-BODY-9f3a-do-not-log"
    with caplog.at_level(logging.DEBUG):
        status, _headers, _payload = _post(base, {"to": "15551234567", "body": canary})
    assert status == 200 and driver.body == canary
    assert canary not in caplog.text
    assert TOKEN not in caplog.text


# ── service: transport selection ───────────────────────────────────────────────────────────


async def test_bridge_mode_selected_when_url_set(bridge, monkeypatch):
    mod, base, driver, _server = bridge
    monkeypatch.setenv(service.BRIDGE_URL_ENV, base)
    monkeypatch.setenv(service.PHONE_SEND_BRIDGE_TOKEN_ENV, TOKEN)

    async def poisoned(*_a, **_kw):  # pragma: no cover - must never run
        raise AssertionError("the direct driver path ran while PHONE_SEND_BRIDGE_URL was set")

    monkeypatch.setattr(service, "_run_driver", poisoned)
    result = await service.send("15551234567", BODY, sandbox=False)
    assert result.sent_at == SENT_PAYLOAD["data"]["sent_at"]
    assert result.to_masked == "••••4477"
    assert result.driver == service.DRIVER_NAME
    assert driver.body == BODY and "--send" in driver.argv


async def test_missing_bridge_token_is_e_config(monkeypatch):
    monkeypatch.setenv(service.BRIDGE_URL_ENV, "http://127.0.0.1:9227")
    monkeypatch.delenv(service.PHONE_SEND_BRIDGE_TOKEN_ENV, raising=False)

    async def poisoned(*_a, **_kw):  # pragma: no cover
        raise AssertionError("a tokenless bridge POST was attempted")

    monkeypatch.setattr(service, "_post_bridge", poisoned)
    with pytest.raises(PolsiaError) as caught:
        await service.send("15551234567", BODY, sandbox=False)
    assert caught.value.code == E_CONFIG
    assert "PHONE_SEND_BRIDGE_TOKEN" in caught.value.message
    assert caught.value.context.get("bridge_url") == "http://127.0.0.1:9227"


async def test_short_bridge_token_is_e_config(monkeypatch):
    monkeypatch.setenv(service.BRIDGE_URL_ENV, "http://127.0.0.1:9227")
    monkeypatch.setenv(service.PHONE_SEND_BRIDGE_TOKEN_ENV, "short")
    with pytest.raises(PolsiaError) as caught:
        await service.send("15551234567", BODY, sandbox=False)
    assert caught.value.code == E_CONFIG


async def test_non_http_bridge_url_is_e_config(monkeypatch):
    monkeypatch.setenv(service.BRIDGE_URL_ENV, "ftp://127.0.0.1:9227")
    monkeypatch.setenv(service.PHONE_SEND_BRIDGE_TOKEN_ENV, TOKEN)
    with pytest.raises(PolsiaError) as caught:
        await service.send("15551234567", BODY, sandbox=False)
    assert caught.value.code == E_CONFIG


async def test_bridge_401_maps_to_e_auth(bridge, monkeypatch):
    _mod, base, _driver, _server = bridge
    monkeypatch.setenv(service.BRIDGE_URL_ENV, base)
    monkeypatch.setenv(service.PHONE_SEND_BRIDGE_TOKEN_ENV, OTHER_TOKEN)
    with pytest.raises(PolsiaError) as caught:
        await service.send("15551234567", BODY, sandbox=False)
    assert caught.value.code == E_AUTH
    assert base in caught.value.message


async def test_bridge_unreachable_names_the_url(monkeypatch):
    url = f"http://127.0.0.1:{_closed_port()}"
    monkeypatch.setenv(service.BRIDGE_URL_ENV, url)
    monkeypatch.setenv(service.PHONE_SEND_BRIDGE_TOKEN_ENV, TOKEN)
    with pytest.raises(PolsiaError) as caught:
        await service.send("15551234567", BODY, sandbox=False)
    assert caught.value.code == E_PHONE_UNAVAILABLE
    assert url in caught.value.message
    assert caught.value.context.get("bridge_url") == url


async def test_bridge_timeout_is_named(monkeypatch):
    stall = _StallServer(("127.0.0.1", 0), _StallHandler)
    thread = threading.Thread(target=stall.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{stall.server_address[1]}"
    monkeypatch.setenv(service.BRIDGE_URL_ENV, url)
    monkeypatch.setenv(service.PHONE_SEND_BRIDGE_TOKEN_ENV, TOKEN)
    try:
        with pytest.raises(PolsiaError) as caught:
            await service.send("15551234567", BODY, sandbox=False, timeout=0.3)
    finally:
        stall.shutdown()
        stall.server_close()
        thread.join(5)
    assert caught.value.code == E_PHONE_UNAVAILABLE
    assert "timed out" in caught.value.message.lower()
    assert url in caught.value.message


async def test_bridge_refusal_maps_to_the_same_typed_code(bridge, monkeypatch):
    mod, base, _driver, _server = bridge
    _stub_driver(monkeypatch, mod, UNPAIRED_PAYLOAD, 3)
    monkeypatch.setenv(service.BRIDGE_URL_ENV, base)
    monkeypatch.setenv(service.PHONE_SEND_BRIDGE_TOKEN_ENV, TOKEN)
    with pytest.raises(PolsiaError) as caught:
        await service.send("15551234567", BODY, sandbox=False)
    assert caught.value.code == E_PHONE_SEND_UNPAIRED
    assert caught.value.how_to_fix


async def test_sandbox_refuses_before_the_bridge(monkeypatch):
    monkeypatch.setenv(service.BRIDGE_URL_ENV, "http://127.0.0.1:9227")
    monkeypatch.setenv(service.PHONE_SEND_BRIDGE_TOKEN_ENV, TOKEN)

    async def poisoned(*_a, **_kw):  # pragma: no cover
        raise AssertionError("a sandboxed send reached the bridge")

    monkeypatch.setattr(service, "_post_bridge", poisoned)
    with pytest.raises(PolsiaError) as caught:
        await service.send("15551234567", BODY, sandbox=True)
    assert caught.value.code == E_SANDBOX_BLOCKED


async def test_direct_mode_is_unchanged_when_url_unset(monkeypatch):
    monkeypatch.delenv(service.BRIDGE_URL_ENV, raising=False)
    monkeypatch.delenv(service.PHONE_SEND_BRIDGE_TOKEN_ENV, raising=False)

    async def poisoned(*_a, **_kw):  # pragma: no cover
        raise AssertionError("the bridge transport ran with no PHONE_SEND_BRIDGE_URL")

    async def fake_driver(argv, timeout):
        return service.DriverRun(0, json.dumps(SENT_PAYLOAD))

    monkeypatch.setattr(service, "_post_bridge", poisoned)
    monkeypatch.setattr(service, "_run_driver", fake_driver)
    result = await service.send("15551234567", BODY, sandbox=False)
    assert result.sent_at == SENT_PAYLOAD["data"]["sent_at"]
    assert result.driver == service.DRIVER_NAME
