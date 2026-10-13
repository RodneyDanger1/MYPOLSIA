"""RD13 — edge tests for the DeepSeek key routes (`/api/v1/config/deepseek-key`).

Boundaries pinned here (the route paths, response models and E_* codes are frozen; nothing
below invents a new surface):

* PUT body validation: missing api_key / non-string / null -> FastAPI's own 422, store untouched;
  whitespace -> the app's named `E_VALIDATION` refusal (422, covered in test_deepseek_key.py too);
* PUT overwrite replaces the stored value everywhere (file, cache, runtime resolver);
* DELETE with ONLY the env configured stays honest (`source=env`), and with nothing configured
  reports `source=none` — never a fake "stored";
* verify with nothing configured refuses by NAME and never calls out;
* verify with a stored key issues exactly `GET https://api.deepseek.com/models` with
  `Authorization: Bearer <key>` and nothing else; the candidate of a verify-before-save probe is
  never stored;
* a corrupt store file answers 503 `E_STORAGE` (named), never "configured: false"; a PUT repairs it;
* no response body, and no log record, ever contains a raw key.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.services import secret_store

RAW_KEY = "sk-rd13-route-raw-0001"
RAW_KEY_2 = "sk-rd13-route-raw-0002"
ENV_KEY = "sk-rd13-route-env-0003"
CANDIDATE = "sk-rd13-candidate-0004"

STATUS_PATH = "/api/v1/config/deepseek-key"
VERIFY_PATH = "/api/v1/config/deepseek-key/verify"


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


def _refusal(body: dict) -> dict:
    """The app emits named refusals in either wire shape (envelope or `detail`)."""
    if body.get("ok") is False and isinstance(body.get("error"), dict):
        return body["error"]
    if isinstance(body.get("detail"), dict) and "code" in body["detail"]:
        return body["detail"]
    return {}


# ── PUT body validation ──────────────────────────────────────────────────────────────────────


async def test_put_without_a_body_key_is_a_422_and_stores_nothing(
    key_file: Path, api_client, auth_headers
) -> None:
    for body in ({}, {"other": "x"}):
        resp = await api_client.put(STATUS_PATH, headers=auth_headers, json=body)
        assert resp.status_code == 422
    assert not key_file.exists()
    assert secret_store.get_deepseek_api_key() is None


@pytest.mark.parametrize("bad_value", [123, None, True, ["sk-x"], {"nested": "sk-x"}])
async def test_put_with_a_non_string_key_is_a_422_and_stores_nothing(
    key_file: Path, api_client, auth_headers, bad_value
) -> None:
    resp = await api_client.put(STATUS_PATH, headers=auth_headers, json={"api_key": bad_value})
    assert resp.status_code == 422
    assert not key_file.exists()
    assert secret_store.get_deepseek_api_key() is None


async def test_put_overwrites_the_previous_stored_key(
    key_file: Path, api_client, auth_headers
) -> None:
    first = await api_client.put(STATUS_PATH, headers=auth_headers, json={"api_key": RAW_KEY})
    assert first.status_code == 200

    second = await api_client.put(STATUS_PATH, headers=auth_headers, json={"api_key": RAW_KEY_2})
    assert second.status_code == 200
    assert second.json() == {
        "configured": True, "source": "stored", "masked": f"{RAW_KEY_2[:3]}…{RAW_KEY_2[-4:]}",
    }
    assert RAW_KEY not in second.text and RAW_KEY_2 not in second.text

    # File, cache and the runtime resolver all agree on the new value.
    assert json.loads(key_file.read_text(encoding="utf-8")) == {"deepseek_api_key": RAW_KEY_2}
    assert secret_store.get_deepseek_api_key() == RAW_KEY_2

    # ... and the overwritten key is gone from the durable store.
    assert RAW_KEY not in key_file.read_text(encoding="utf-8")


async def test_put_of_a_short_key_masks_to_the_bare_ellipsis_honestly(
    key_file: Path, api_client, auth_headers
) -> None:
    """A 4-char value is not blank, so it is storable — and the mask reveals nothing."""
    resp = await api_client.put(STATUS_PATH, headers=auth_headers, json={"api_key": "abcd"})
    assert resp.status_code == 200
    assert resp.json() == {"configured": True, "source": "stored", "masked": "…"}
    assert "abcd" not in resp.text
    assert secret_store.get_deepseek_api_key() == "abcd"


# ── DELETE honesty ───────────────────────────────────────────────────────────────────────────


async def test_delete_with_only_the_env_configured_stays_source_env(
    key_file: Path, api_client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(secret_store.DEEPSEEK_ENV_VAR, ENV_KEY)
    resp = await api_client.delete(STATUS_PATH, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "configured": True, "source": "env", "masked": f"{ENV_KEY[:3]}…{ENV_KEY[-4:]}",
    }
    assert not key_file.exists()  # nothing was ever stored — and none was fabricated
    assert secret_store.get_deepseek_api_key() == ENV_KEY


async def test_delete_with_nothing_configured_reports_none_honestly(
    key_file: Path, api_client, auth_headers
) -> None:
    resp = await api_client.delete(STATUS_PATH, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "source": "none", "masked": "…"}


# ── verify: named refusal, exact request shape, no side effects ──────────────────────────────


async def test_verify_with_nothing_configured_refuses_by_name_without_calling_out(
    key_file: Path, api_client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_stub(
        lambda url, headers: _StubResponse(200, {"data": []}), monkeypatch
    )
    resp = await api_client.post(VERIFY_PATH, headers=auth_headers, json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "nothing to verify" in body["detail"]
    assert calls == []  # an empty key must never leave the process


async def test_verify_with_a_stored_key_issues_exactly_the_documented_request(
    key_file: Path, api_client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"object": "list", "data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]}
    calls = _install_stub(lambda url, headers: _StubResponse(200, payload), monkeypatch)
    await api_client.put(STATUS_PATH, headers=auth_headers, json={"api_key": RAW_KEY})

    resp = await api_client.post(VERIFY_PATH, headers=auth_headers, json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and "deepseek-flash" in body["detail"]
    assert len(calls) == 1
    assert calls[0]["url"] == secret_store.DEEPSEEK_MODELS_URL
    assert calls[0]["headers"]["Authorization"] == f"Bearer {RAW_KEY}"
    assert "?" not in str(calls[0]["url"])  # the key never rides a query string
    assert RAW_KEY not in resp.text


async def test_verify_before_save_probes_the_candidate_and_stores_nothing(
    key_file: Path, api_client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_stub(
        lambda url, headers: _StubResponse(200, {"data": [{"id": "deepseek-flash"}]}),
        monkeypatch,
    )
    resp = await api_client.post(VERIFY_PATH, headers=auth_headers, json={"api_key": CANDIDATE})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert calls[0]["headers"]["Authorization"] == f"Bearer {CANDIDATE}"
    assert not key_file.exists()  # verify-before-save has no side effect
    assert secret_store.get_deepseek_api_key() is None


# ── a corrupt store file is a NAMED 503, and PUT repairs it ──────────────────────────────────


async def test_corrupt_store_file_is_a_named_503_not_a_fake_unconfigured(
    key_file: Path, api_client, auth_headers
) -> None:
    key_file.write_text('{"deepseek_api_key": 123}', encoding="utf-8")

    resp = await api_client.get(STATUS_PATH, headers=auth_headers)
    assert resp.status_code == 503
    detail = _refusal(resp.json())
    assert detail.get("code") == "E_STORAGE"
    assert detail.get("how_to_fix")
    assert "configured" not in resp.json()  # never a plausible fake status

    # PUT rewrites the file atomically — the operator's recovery path works without shell access.
    put = await api_client.put(STATUS_PATH, headers=auth_headers, json={"api_key": RAW_KEY})
    assert put.status_code == 200
    assert json.loads(key_file.read_text(encoding="utf-8")) == {"deepseek_api_key": RAW_KEY}
    after = await api_client.get(STATUS_PATH, headers=auth_headers)
    assert after.json()["source"] == "stored"


# ── the key never leaks: bodies and logs ─────────────────────────────────────────────────────


async def test_no_route_body_or_log_record_ever_carries_a_raw_key(
    key_file: Path, api_client, auth_headers, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    def echo_received_key(url: str, headers: dict[str, str]) -> _StubResponse:
        """The realistic hostile-provider shape: echo the credential that was actually sent."""
        received = str(headers.get("Authorization", "")).removeprefix("Bearer ")
        payload = {"error": {"message": f"bad key {received} rejected"}}
        return _StubResponse(401, payload, text=json.dumps(payload))

    _install_stub(echo_received_key, monkeypatch)

    bodies: list[str] = []
    with caplog.at_level(logging.DEBUG):
        put = await api_client.put(STATUS_PATH, headers=auth_headers, json={"api_key": RAW_KEY})
        bodies.append(put.text)
        overwrite = await api_client.put(
            STATUS_PATH, headers=auth_headers, json={"api_key": RAW_KEY_2}
        )
        bodies.append(overwrite.text)
        get = await api_client.get(STATUS_PATH, headers=auth_headers)
        bodies.append(get.text)
        verify = await api_client.post(VERIFY_PATH, headers=auth_headers, json={})
        bodies.append(verify.text)
        verify_candidate = await api_client.post(
            VERIFY_PATH, headers=auth_headers, json={"api_key": CANDIDATE}
        )
        bodies.append(verify_candidate.text)
        delete = await api_client.delete(STATUS_PATH, headers=auth_headers)
        bodies.append(delete.text)

    joined = "\n".join(bodies)
    for secret in (RAW_KEY, RAW_KEY_2, CANDIDATE):
        assert secret not in joined, "raw key leaked into a response body"
        assert secret not in caplog.text, "raw key leaked into a log record"
    # The hostile provider echo was scrubbed, not swallowed.
    assert verify_candidate.json()["ok"] is False
    assert "(key hidden)" in verify_candidate.json()["detail"]


async def test_verify_route_validates_a_non_string_candidate(
    key_file: Path, api_client, auth_headers
) -> None:
    resp = await api_client.post(VERIFY_PATH, headers=auth_headers, json={"api_key": 123})
    assert resp.status_code == 422
