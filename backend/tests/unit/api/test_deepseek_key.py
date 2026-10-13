"""Endpoint tests for the DeepSeek key lifecycle (`/api/v1/config/deepseek-key`, RD9-keyonboard).

What is pinned here:

* the round trip (PUT → GET → DELETE) and the durable file behind it;
* **the raw key never leaves the API** — not in a response body, not in a log record;
* the blank-key refusal is the app's named-refusal shape (`E_VALIDATION`, 422);
* precedence: a stored key wins over `DEEPSEEK_API_KEY`, and DELETE falls back to it;
* verify reports the REAL probe result (HTTP layer stubbed in the test only) and never
  stores the candidate it was asked to check.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from app.services import secret_store

RAW_KEY = "sk-rd9-endpoint-0000-1111-2222-333344445555"
ENV_KEY = "sk-rd9-env-9999-8888-7777-666655554444"


@pytest.fixture
def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "deepseek_api_key.json"
    monkeypatch.setenv(secret_store.KEY_FILE_ENV_VAR, str(path))
    monkeypatch.delenv(secret_store.DEEPSEEK_ENV_VAR, raising=False)
    return path


class _StubResponse:
    def __init__(self, status_code: int, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


def _install_stub(handler, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Replace only the socket: assert the real URL/headers the probe sends."""
    calls: list[dict[str, object]] = []

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._kwargs = kwargs

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

        async def get(self, url: str, headers: dict[str, str] | None = None):
            calls.append({"url": url, "headers": headers or {}})
            return handler(url, headers or {})

    monkeypatch.setattr(secret_store.httpx, "AsyncClient", _Client)
    return calls


# ── status + round trip ───────────────────────────────────────────────────────────────────────


async def test_get_reports_unconfigured(key_file: Path, api_client, auth_headers) -> None:
    resp = await api_client.get("/api/v1/config/deepseek-key", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "source": "none", "masked": "…"}


async def test_put_round_trips_masks_and_persists(
    key_file: Path, api_client, auth_headers
) -> None:
    resp = await api_client.put(
        "/api/v1/config/deepseek-key", headers=auth_headers, json={"api_key": RAW_KEY}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "configured": True,
        "source": "stored",
        "masked": f"{RAW_KEY[:3]}…{RAW_KEY[-4:]}",
    }
    assert RAW_KEY not in resp.text

    got = await api_client.get("/api/v1/config/deepseek-key", headers=auth_headers)
    assert got.json()["configured"] is True and got.json()["source"] == "stored"
    assert RAW_KEY not in got.text

    # Durable: the value is on disk (that is what survives a restart).
    assert json.loads(key_file.read_text(encoding="utf-8")) == {"deepseek_api_key": RAW_KEY}


async def test_status_responses_expose_only_the_documented_fields(
    key_file: Path, api_client, auth_headers
) -> None:
    await api_client.put(
        "/api/v1/config/deepseek-key", headers=auth_headers, json={"api_key": RAW_KEY}
    )
    resp = await api_client.get("/api/v1/config/deepseek-key", headers=auth_headers)
    assert set(resp.json()) == {"configured", "source", "masked"}


async def test_put_blank_is_refused_with_the_named_validation_shape(
    key_file: Path, api_client, auth_headers
) -> None:
    def refusal(body: dict) -> dict:
        """The app emits named refusals in EITHER wire shape (D5/audit L5-01): the envelope
        `{"ok": false, "error": {code, ...}}` (PolsiaError + the coded-HTTPException handler)
        or `{"detail": {code, ...}}`; the frontend's `refusalFrom` parses both.  This mirrors
        that documented contract instead of pinning one wording of it."""
        if body.get("ok") is False and isinstance(body.get("error"), dict):
            return body["error"]
        if isinstance(body.get("detail"), dict) and "code" in body["detail"]:
            return body["detail"]
        return {}

    for blank in ("", "   "):
        resp = await api_client.put(
            "/api/v1/config/deepseek-key", headers=auth_headers, json={"api_key": blank}
        )
        assert resp.status_code == 422
        detail = refusal(resp.json())
        assert detail.get("code") == "E_VALIDATION"
        assert detail.get("how_to_fix")  # a refusal must ship its fix hint
    assert not key_file.exists()  # a refused write must not create the file


# ── precedence ────────────────────────────────────────────────────────────────────────────────


async def test_stored_key_beats_the_environment_key(
    key_file: Path, api_client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(secret_store.DEEPSEEK_ENV_VAR, ENV_KEY)

    env_status = await api_client.get("/api/v1/config/deepseek-key", headers=auth_headers)
    assert env_status.json()["source"] == "env"
    assert ENV_KEY not in env_status.text

    await api_client.put(
        "/api/v1/config/deepseek-key", headers=auth_headers, json={"api_key": RAW_KEY}
    )
    stored_status = await api_client.get("/api/v1/config/deepseek-key", headers=auth_headers)
    assert stored_status.json()["source"] == "stored"
    # The runtime read agrees with the API: the stored value is what agents would use.
    assert secret_store.get_deepseek_api_key() == RAW_KEY


async def test_delete_falls_back_to_the_environment(
    key_file: Path, api_client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(secret_store.DEEPSEEK_ENV_VAR, ENV_KEY)
    await api_client.put(
        "/api/v1/config/deepseek-key", headers=auth_headers, json={"api_key": RAW_KEY}
    )

    resp = await api_client.delete("/api/v1/config/deepseek-key", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "configured": True,
        "source": "env",
        "masked": f"{ENV_KEY[:3]}…{ENV_KEY[-4:]}",
    }
    assert not key_file.exists()
    assert secret_store.get_deepseek_api_key() == ENV_KEY

    # With the env var gone too, the app is honestly "not configured".
    os.environ.pop(secret_store.DEEPSEEK_ENV_VAR)
    empty = await api_client.get("/api/v1/config/deepseek-key", headers=auth_headers)
    assert empty.json() == {"configured": False, "source": "none", "masked": "…"}


# ── the key never leaks ───────────────────────────────────────────────────────────────────────


async def test_the_key_never_appears_in_logs_or_bodies(
    key_file: Path, api_client, auth_headers, caplog: pytest.LogCaptureFixture
) -> None:
    bodies: list[str] = []
    with caplog.at_level(logging.DEBUG):
        put = await api_client.put(
            "/api/v1/config/deepseek-key", headers=auth_headers, json={"api_key": RAW_KEY}
        )
        bodies.append(put.text)
        get = await api_client.get("/api/v1/config/deepseek-key", headers=auth_headers)
        bodies.append(get.text)
        delete = await api_client.delete("/api/v1/config/deepseek-key", headers=auth_headers)
        bodies.append(delete.text)

    for body in bodies:
        assert RAW_KEY not in body

    for record in caplog.records:
        assert RAW_KEY not in record.getMessage(), f"key leaked into {record.name} log"
    assert RAW_KEY not in caplog.text


# ── verify: real result, honest failure, no side effects ──────────────────────────────────────


async def test_verify_reports_a_real_401_and_does_not_store_the_candidate(
    key_file: Path, api_client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"error": {"message": "Authentication Fails, Your api key is invalid"}}
    calls = _install_stub(
        lambda url, headers: _StubResponse(401, payload, text=json.dumps(payload)), monkeypatch
    )

    resp = await api_client.post(
        "/api/v1/config/deepseek-key/verify",
        headers=auth_headers,
        json={"api_key": "sk-rd9-verify-candidate-4242"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "HTTP 401" in body["detail"]
    assert "sk-rd9-verify-candidate-4242" not in resp.text
    assert calls[0]["url"] == secret_store.DEEPSEEK_MODELS_URL
    # Verify-before-save must not have a side effect: nothing was stored.
    assert secret_store.get_deepseek_api_key() is None


async def test_verify_success_reports_the_live_result(
    key_file: Path, api_client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        lambda url, headers: _StubResponse(
            200, {"object": "list", "data": [{"id": "deepseek-chat"}]}
        ),
        monkeypatch,
    )
    await api_client.put(
        "/api/v1/config/deepseek-key", headers=auth_headers, json={"api_key": RAW_KEY}
    )

    resp = await api_client.post("/api/v1/config/deepseek-key/verify", headers=auth_headers, json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and "deepseek-chat" in body["detail"]
    assert set(body) == {"ok", "detail"}


async def test_verify_without_any_key_is_honestly_false(
    key_file: Path, api_client, auth_headers
) -> None:
    resp = await api_client.post("/api/v1/config/deepseek-key/verify", headers=auth_headers, json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "nothing to verify" in body["detail"]


# ── auth ──────────────────────────────────────────────────────────────────────────────────────


async def test_key_routes_require_the_api_key(key_file: Path, api_client) -> None:
    for method, path in (
        ("get", "/api/v1/config/deepseek-key"),
        ("put", "/api/v1/config/deepseek-key"),
        ("delete", "/api/v1/config/deepseek-key"),
        ("post", "/api/v1/config/deepseek-key/verify"),
    ):
        resp = await getattr(api_client, method)(path)
        assert resp.status_code == 401, f"{method.upper()} {path} is unauthenticated"
