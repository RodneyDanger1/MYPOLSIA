"""Unit tests for `app.services.secret_store` (RD9-keyonboard).

The frozen interface: precedence (stored > env), the masked status (never the key),
atomic persistence, cache invalidation on set/clear, and an HONEST verify probe whose
HTTP layer is stubbed *inside the test* (the production call is real — see
`verify_deepseek_key`; only the socket is replaced here).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from app.core.errors import E_STORAGE, E_VALIDATION, PolsiaError
from app.services import secret_store

#: A clearly fake key.  Distinctive enough that "does it leak?" tests are meaningful;
#: shaped like a real DeepSeek key (sk- prefix) so masking behaves as in production.
FAKE_KEY = "sk-rd9-secret-store-0000-1111-2222-333344445555"
FAKE_ENV_KEY = "sk-rd9-env-fallback-9999-8888-7777-666655554444"


@pytest.fixture
def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a scratch file and clear the env fallback for the test."""
    path = tmp_path / "deepseek_api_key.json"
    monkeypatch.setenv(secret_store.KEY_FILE_ENV_VAR, str(path))
    monkeypatch.delenv(secret_store.DEEPSEEK_ENV_VAR, raising=False)
    return path


# ── precedence + round trip ───────────────────────────────────────────────────────────────────


def test_nothing_configured_returns_none_and_an_honest_status(key_file: Path) -> None:
    assert secret_store.get_deepseek_api_key() is None
    assert secret_store.deepseek_key_status() == {
        "configured": False,
        "source": "none",
        "masked": "…",
    }


def test_set_then_get_round_trips_and_persists_to_disk(key_file: Path) -> None:
    secret_store.set_deepseek_api_key(f"  {FAKE_KEY}  ")  # whitespace is stripped, not stored

    assert secret_store.get_deepseek_api_key() == FAKE_KEY
    assert secret_store.deepseek_key_status()["source"] == "stored"

    on_disk = json.loads(key_file.read_text(encoding="utf-8"))
    assert on_disk == {"deepseek_api_key": FAKE_KEY}


def test_env_is_the_fallback_and_stored_wins_over_env(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(secret_store.DEEPSEEK_ENV_VAR, FAKE_ENV_KEY)

    assert secret_store.get_deepseek_api_key() == FAKE_ENV_KEY
    status = secret_store.deepseek_key_status()
    assert status["source"] == "env" and status["configured"] is True

    secret_store.set_deepseek_api_key(FAKE_KEY)
    # Precedence: the value the owner typed in the app beats deployment state.
    assert secret_store.get_deepseek_api_key() == FAKE_KEY
    assert secret_store.deepseek_key_status()["source"] == "stored"

    secret_store.clear_deepseek_api_key()
    # Removing the stored value falls back to the environment, it does not unconfigure the app.
    assert secret_store.get_deepseek_api_key() == FAKE_ENV_KEY
    assert secret_store.deepseek_key_status()["source"] == "env"


def test_blank_values_are_refused_and_nothing_is_written(key_file: Path) -> None:
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(PolsiaError) as excinfo:
            secret_store.set_deepseek_api_key(blank)
        assert excinfo.value.code == E_VALIDATION
    assert not key_file.exists()


def test_clear_on_an_empty_store_is_not_an_error(key_file: Path) -> None:
    secret_store.clear_deepseek_api_key()  # must not raise
    assert secret_store.get_deepseek_api_key() is None


# ── masking ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sk-1234567890abcdef", "sk-…cdef"),
        ("short", "…hort"),
        ("abcde", "…bcde"),
        ("abcd", "…"),
        ("", "…"),
    ],
)
def test_masking_shapes(value: str, expected: str) -> None:
    assert secret_store.mask_secret(value) == expected


def test_masking_never_reveals_the_whole_secret() -> None:
    for value in (FAKE_KEY, "sk-1234567890abcdef", "abcdefghijkl", "abcdefghijk"):
        masked = secret_store.mask_secret(value)
        assert masked != value
        assert len(masked) < len(value)
        # The middle of the secret is never part of the masked form (when it has one).
        middle = value[4:-4]
        assert middle == "" or middle not in masked


# ── atomic write + cache ──────────────────────────────────────────────────────────────────────


def test_write_leaves_no_temp_file_behind(key_file: Path) -> None:
    secret_store.set_deepseek_api_key(FAKE_KEY)
    leftovers = [p.name for p in key_file.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_a_failed_replace_keeps_the_previous_value(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_store.set_deepseek_api_key(FAKE_KEY)

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(secret_store.os, "replace", _explode)

    with pytest.raises(PolsiaError) as excinfo:
        secret_store.set_deepseek_api_key("sk-rd9-second-value-should-not-land")
    assert excinfo.value.code == E_STORAGE

    # The old value is still live and the store is not left half-written.
    assert secret_store.get_deepseek_api_key() == FAKE_KEY
    assert json.loads(key_file.read_text(encoding="utf-8"))["deepseek_api_key"] == FAKE_KEY
    assert [p.name for p in key_file.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_cache_is_invalidated_by_set_and_clear(key_file: Path) -> None:
    assert secret_store.get_deepseek_api_key() is None  # caches the miss

    secret_store.set_deepseek_api_key(FAKE_KEY)
    assert secret_store.get_deepseek_api_key() == FAKE_KEY  # set() invalidated the miss

    # An out-of-band edit is NOT observed (documented: the cache is path-keyed and only
    # set/clear invalidate) — this pins the documented behaviour instead of pretending.
    key_file.write_text(json.dumps({"deepseek_api_key": "sk-rd9-out-of-band"}), encoding="utf-8")
    assert secret_store.get_deepseek_api_key() == FAKE_KEY

    secret_store.set_deepseek_api_key("sk-rd9-cache-invalidation-4444")  # set() re-reads fresh
    assert secret_store.get_deepseek_api_key() == "sk-rd9-cache-invalidation-4444"

    secret_store.clear_deepseek_api_key()
    assert secret_store.get_deepseek_api_key() is None


def test_a_corrupt_file_is_a_named_storage_refusal_not_a_silent_env_fallback(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file.write_text("this is not json", encoding="utf-8")
    monkeypatch.setenv(secret_store.DEEPSEEK_ENV_VAR, FAKE_ENV_KEY)

    with pytest.raises(PolsiaError) as excinfo:
        secret_store.get_deepseek_api_key()
    assert excinfo.value.code == E_STORAGE
    # Reporting "env" here would blame the wrong credential; the store refuses instead.
    assert secret_store.DEEPSEEK_ENV_VAR not in str(excinfo.value)


# ── verify: a REAL probe, an honest result ────────────────────────────────────────────────────


class _StubClient:
    """Test double for `httpx.AsyncClient` — replaces the socket, nothing else.

    `handler(url, headers)` returns an `httpx.Response`-alike or raises.  Every call is
    recorded so the test can assert the URL, the Bearer header and the timeout actually sent.
    """

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict[str, object]] = []

    def _client_factory(self):
        outer = self

        class _Client:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._kwargs = kwargs

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *exc_info: object) -> bool:
                return False

            async def get(self, url: str, headers: dict[str, str] | None = None):
                outer.calls.append(
                    {"url": url, "headers": headers or {}, "timeout": self._kwargs.get("timeout")}
                )
                return outer._handler(url, headers or {})

        return _Client

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _StubClient:
        monkeypatch.setattr(secret_store.httpx, "AsyncClient", self._client_factory())
        return self


class _StubResponse:
    def __init__(self, status_code: int, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


async def test_verify_with_no_key_makes_no_call_and_says_so(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _StubClient(lambda url, headers: _StubResponse(200, {"data": []})).install(monkeypatch)
    result = await secret_store.verify_deepseek_key()
    assert result["ok"] is False
    assert "nothing to verify" in result["detail"]
    assert stub.calls == []  # an empty key must never leave the process


async def test_verify_uses_the_candidate_key_with_real_bearer_auth_and_reports_success(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = "sk-rd9-candidate-1234-5678"
    payload = {"object": "list", "data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}
    stub = _StubClient(lambda url, headers: _StubResponse(200, payload)).install(monkeypatch)

    result = await secret_store.verify_deepseek_key(candidate)

    assert result["ok"] is True
    assert "key accepted" in result["detail"] and "deepseek-chat" in result["detail"]
    assert len(stub.calls) == 1
    assert stub.calls[0]["url"] == secret_store.DEEPSEEK_MODELS_URL
    assert stub.calls[0]["headers"]["Authorization"] == f"Bearer {candidate}"
    assert stub.calls[0]["timeout"] == secret_store.VERIFY_TIMEOUT_SECONDS


async def test_verify_falls_back_to_the_stored_key_when_no_candidate(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_store.set_deepseek_api_key(FAKE_KEY)
    stub = _StubClient(lambda url, headers: _StubResponse(200, {"data": []})).install(monkeypatch)

    result = await secret_store.verify_deepseek_key("   ")  # blank candidate == none

    assert result["ok"] is True
    assert stub.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"


async def test_verify_reports_an_http_401_honestly_and_never_echoes_the_key(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = "sk-rd9-rejected-0000-1111"
    payload = {"error": {"message": "Authentication Fails, Your api key is invalid"}}
    _StubClient(lambda url, headers: _StubResponse(401, payload, text=json.dumps(payload))).install(
        monkeypatch
    )

    result = await secret_store.verify_deepseek_key(candidate)

    assert result["ok"] is False
    assert "HTTP 401" in result["detail"] and "Authentication Fails" in result["detail"]
    assert candidate not in result["detail"]


async def test_verify_scrubs_a_key_echoed_by_an_adversarial_provider(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = "sk-rd9-echoed-0011-2233"
    echoed = {"error": {"message": f"invalid key {candidate} — please check"}}
    _StubClient(lambda url, headers: _StubResponse(403, echoed, text=json.dumps(echoed))).install(
        monkeypatch
    )

    result = await secret_store.verify_deepseek_key(candidate)

    assert result["ok"] is False
    assert candidate not in result["detail"]
    assert "(key hidden)" in result["detail"]


async def test_verify_reports_a_timeout_honestly(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(url: str, headers: dict[str, str]):
        raise httpx.TimeoutException("simulated timeout")

    _StubClient(_boom).install(monkeypatch)
    result = await secret_store.verify_deepseek_key(FAKE_KEY)
    assert result["ok"] is False
    assert "timed out" in result["detail"]


async def test_verify_reports_a_transport_failure_honestly(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(url: str, headers: dict[str, str]):
        raise httpx.ConnectError("simulated DNS failure")

    _StubClient(_boom).install(monkeypatch)
    result = await secret_store.verify_deepseek_key(FAKE_KEY)
    assert result["ok"] is False
    assert "ConnectError" in result["detail"]
    assert FAKE_KEY not in result["detail"]


async def test_verify_never_sends_the_key_anywhere_but_the_authorization_header(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe keeps nothing else: URL is the documented one, and no query string."""
    stub = _StubClient(lambda url, headers: _StubResponse(200, {"data": []})).install(monkeypatch)
    await secret_store.verify_deepseek_key(FAKE_KEY)
    assert "?" not in stub.calls[0]["url"]
    assert sorted(stub.calls[0]["headers"]) == ["Accept", "Authorization"]


# ── the default path (what the app uses when nothing is configured) ───────────────────────────


def test_default_store_path_is_the_documented_data_area(monkeypatch: pytest.MonkeyPatch) -> None:
    """RE-PINNED by fix-5 / L1-07 — the DEFAULT path is now ANCHORED at the backend dir.

    The old assertion pinned the cwd-relative literal (`Path("data/deepseek_api_key.json")`),
    which is the defect: launched from the repo root the store landed at `<repo>/data/…` — a
    path no `.gitignore` rule covers — while `.gitignore`, the compose volume and the docs all
    assume `backend/data/…`.  The documented data area is unchanged; only its resolution is
    anchored (the D7 treatment `chroma_client._fallback_persist_dir` already applies).
    """
    monkeypatch.delenv(secret_store.KEY_FILE_ENV_VAR, raising=False)
    monkeypatch.setattr(secret_store.settings, "deepseek_key_file", "", raising=False)

    resolved = secret_store._resolve_key_file()
    assert resolved == (
        Path(secret_store.__file__).resolve().parents[2] / "data" / "deepseek_api_key.json"
    )
    assert os.path.basename(str(resolved)) == "deepseek_api_key.json"
