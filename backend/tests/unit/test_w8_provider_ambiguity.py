"""WAVE 8 / FORGE — the provider ambiguity class for the remaining adapters, the error matrix,
and the DeepSeek per-call bound.

Every test here proves behaviour that was WRONG or UNPROVEN before wave 8:

  A. The social-post adapters (X/Twitter, LinkedIn, Facebook, Instagram, TikTok, YouTube), the
     PyGithub pull-request adapter and the Stripe refund adapter flattened a NO-ANSWER transport
     fault into the same bare "failed" refusal as a provider that ANSWERED with an error.  For a
     mutation that is a lie in the dangerous direction: the post / PR / refund may already exist,
     and a blind retry performs it twice.  Each SDK's exception is now classified at its own edge
     and the no-answer case raises `ProviderTransportError`, which `integration_call` reports as
     `E_INTEGRATION` + `outcome_unknown: True` + "check before retrying".
  B. Three calls were UNBOUNDED or inherited the SDK's own long/shared bound: tweepy passes no
     timeout to `requests` at all, PyGithub defaults to a 10-attempt urllib3 retry that includes
     POST, and Stripe 11.3 defaults to an 80 s socket timeout with `max_network_retries = 2`.
     The adapters now own the bound (and Stripe's retry count), and each refusal NAMES it.
  C. The error matrix (provider failure class -> named E_* code -> HTTP status) is proven through
     the ONE real `PolsiaError` handler, not a re-implementation.

Doubles live ONLY at the third-party edges (a fake `tweepy`/`github`/`stripe` module, the `httpx`
module's `post`/`stream` callables, `requests.Session.request`); the code under test is the real
adapter, the real `integration_call`, and the real HTTP handler.  The pinned real SDKs are also
built once each (no network on construction) so an SDK bump fails a contract test HERE instead of
silently restoring an unbounded or auto-retrying mutation.
"""
from __future__ import annotations

import inspect
import json
import sys
import types
from typing import Any

import httpx
import pytest
import requests

import app.config as app_config
from app.agents import claude_cli, deepseek_runtime, integrations
from app.core.errors import (
    E_AUTH,
    E_CLI_TIMEOUT,
    E_CONFIG,
    E_INTEGRATION,
    E_UPSTREAM,
    HTTP_STATUS_BY_CODE,
    PolsiaError,
    http_status_for,
)
from app.services.github_service import GITHUB_TIMEOUT_S

TWITTER_CREDS = {
    "twitter_api_key": "w8-key",
    "twitter_api_secret": "w8-secret",
    "twitter_access_token": "w8-token",
    "twitter_access_secret": "w8-access-secret",
}
LINKEDIN_CREDS = {"linkedin_access_token": "w8-li-token", "linkedin_author_urn": "urn:li:person:w8"}
INSTAGRAM_CREDS = {"meta_page_access_token": "w8-meta-token", "meta_instagram_user_id": "1784"}
TIKTOK_CREDS = {"tiktok_access_token": "w8-tt-token"}
YOUTUBE_CREDS = {"youtube_access_token": "w8-yt-token"}
MEDIA_URL = "https://cdn.example.com/w8.mp4"


def _set(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for field, value in values.items():
        monkeypatch.setattr(app_config.settings, field, value)


class _FakeResponse:
    """The httpx/requests response shape the adapters read — status, headers, text, json."""

    def __init__(
        self,
        status_code: int = 200,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


def _patch_httpx_post(
    monkeypatch: pytest.MonkeyPatch, sequencer: list[Any]
) -> list[dict]:
    """Replace `httpx.post` with a recorder; each call consumes the next item.

    An item that is an exception instance is RAISED (a transport fault at the third-party edge);
    anything else is returned as the response.
    """
    calls: list[dict] = []

    def _post(url: str, **kwargs: Any) -> Any:
        calls.append({"url": url, **kwargs})
        item = sequencer.pop(0) if len(sequencer) > 1 else sequencer[0]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(httpx, "post", _post)
    return calls


def _patch_httpx_stream(
    monkeypatch: pytest.MonkeyPatch, *, fault: BaseException | None = None, media: bytes = b"w8-video"
) -> list[dict]:
    """Replace `httpx.stream(...)` (the YouTube media download) with a bounded recorder."""
    calls: list[dict] = []

    class _Stream:
        status_code = 200
        headers = {"content-type": "video/mp4"}
        text = ""

        def __enter__(self) -> _Stream:
            return self

        def __exit__(self, *exc_info: Any) -> bool:
            return False

        def iter_bytes(self) -> Any:
            yield media

        def read(self, *args: Any) -> bytes:
            return media

    def _stream(method: str, url: str, **kwargs: Any) -> _Stream:
        calls.append({"method": method, "url": url, **kwargs})
        if fault is not None:
            raise fault
        return _Stream()

    monkeypatch.setattr(httpx, "stream", _stream)
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# A. Social-post adapters: no answer -> the named ambiguity class
# ─────────────────────────────────────────────────────────────────────────────

def test_linkedin_no_answer_is_the_named_unknown_outcome(monkeypatch):
    """RED before wave 8: an httpx transport fault was reported as a bare "failed"."""
    _set(monkeypatch, **LINKEDIN_CREDS)
    calls = _patch_httpx_post(monkeypatch, [httpx.ReadTimeout("read timed out w8")])

    out = integrations.post_tweet("hello w8", platform="linkedin")

    assert out["ok"] is False, out
    error = out["error"]
    assert error["code"] == E_INTEGRATION
    assert error["context"]["outcome_unknown"] is True
    assert "may have reached the provider" in error["message"]
    assert "check the provider's own activity log before retrying" in error["message"]
    # The BOUND is named, not just "a timeout".
    assert "LinkedIn produced no answer within 30s" in error["message"]
    assert calls[0]["timeout"] == integrations.SOCIAL_POST_TIMEOUT_SECONDS == 30.0


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_a_social_provider_answer_keeps_its_status_and_is_never_a_success(monkeypatch, status):
    """The provider ANSWERED: the status travels in the refusal and the outcome is NOT unknown."""
    _set(monkeypatch, **LINKEDIN_CREDS)
    _patch_httpx_post(monkeypatch, [_FakeResponse(status, text=f"provider says {status}")])

    out = integrations.post_tweet("hello w8", platform="linkedin")

    assert out["ok"] is False
    error = out["error"]
    assert error["code"] == E_INTEGRATION
    assert "outcome_unknown" not in error["context"]
    assert str(status) in error["message"]
    assert "unknown outcome" not in error["message"]


def test_twitter_no_answer_is_the_named_unknown_outcome(monkeypatch):
    """RED before wave 8: tweepy's transport exception was flattened into "failed"."""
    _set(monkeypatch, **TWITTER_CREDS)

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            self.session = requests.Session()

        def create_tweet(self, *, text: str, **kwargs: Any) -> Any:
            raise requests.ConnectionError("connection refused w8")

    _install_tweepy(monkeypatch, _Client)

    out = integrations.post_tweet("hello w8")

    assert out["ok"] is False
    error = out["error"]
    assert error["code"] == E_INTEGRATION
    assert error["context"]["outcome_unknown"] is True
    assert "X/Twitter produced no answer within 20s" in error["message"]
    assert "may have reached the provider" in error["message"]


def test_a_tweepy_http_exception_is_a_provider_answer_not_an_unknown_outcome(monkeypatch):
    """`tweepy.errors.HTTPException` carries the provider's response — it is NOT a transport fault."""
    _set(monkeypatch, **TWITTER_CREDS)

    class _HTTPException(Exception):
        def __init__(self, response: _FakeResponse) -> None:
            super().__init__(f"401 Unauthorized: {response.text}")
            self.response = response

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            self.session = requests.Session()

        def create_tweet(self, *, text: str, **kwargs: Any) -> Any:
            raise _HTTPException(_FakeResponse(401, text="invalid token"))

    _install_tweepy(monkeypatch, _Client)
    out = integrations.post_tweet("hello w8")

    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert "outcome_unknown" not in out["error"]["context"]
    assert "401" in out["error"]["message"]


def test_the_twitter_call_is_bounded_on_the_session_tweepy_does_not_bound(monkeypatch):
    """RED before wave 8: tweepy passes NO timeout to requests -> an unbounded post."""
    _set(monkeypatch, **TWITTER_CREDS)
    seen: dict[str, Any] = {}

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            self.session = requests.Session()

        def create_tweet(self, *, text: str, **kwargs: Any) -> Any:
            self.session.request("POST", "https://api.twitter.com/2/tweets", json={"text": text})
            return types.SimpleNamespace(data={"id": 42})

    _install_tweepy(monkeypatch, _Client)

    def _fake_request(self: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return _FakeResponse(200)

    monkeypatch.setattr(requests.Session, "request", _fake_request)
    out = integrations.post_tweet("hello w8")

    assert out == {"ok": True, "result": {"id": "42"}}
    assert seen["timeout"] == integrations.TWITTER_TIMEOUT_SECONDS == 20.0


def test_instagram_publish_no_answer_is_the_named_unknown_outcome(monkeypatch):
    _set(monkeypatch, **INSTAGRAM_CREDS)
    _patch_httpx_post(
        monkeypatch,
        [_FakeResponse(200, {"id": "container-w8"}), httpx.WriteError("connection reset w8")],
    )

    out = integrations.post_tweet("hi w8", platform="instagram", image_url=MEDIA_URL)

    assert out["ok"] is False
    assert out["error"]["context"]["outcome_unknown"] is True
    assert "Instagram produced no answer within 30s" in out["error"]["message"]


def test_instagram_container_no_answer_is_not_an_unknown_outcome(monkeypatch):
    """A fault BEFORE the publish leg cannot have posted: no claim of a possibly-done post."""
    _set(monkeypatch, **INSTAGRAM_CREDS)
    _patch_httpx_post(monkeypatch, [httpx.ReadTimeout("container read timed out w8")])

    out = integrations.post_tweet("hi w8", platform="instagram", image_url=MEDIA_URL)

    assert out["ok"] is False
    assert "outcome_unknown" not in out["error"]["context"]
    assert "no answer" not in out["error"]["message"]
    assert "container read timed out w8" in out["error"]["message"]


def test_tiktok_no_answer_is_the_named_unknown_outcome(monkeypatch):
    _set(monkeypatch, **TIKTOK_CREDS)
    _patch_httpx_post(monkeypatch, [httpx.RemoteProtocolError("peer closed w8")])

    out = integrations.post_tweet("hi w8", platform="tiktok", image_url=MEDIA_URL)

    assert out["ok"] is False
    assert out["error"]["context"]["outcome_unknown"] is True
    assert "TikTok produced no answer within 30s" in out["error"]["message"]


def test_youtube_media_download_no_answer_is_not_an_unknown_outcome(monkeypatch):
    """The download happens BEFORE videos.insert: a fault there published nothing."""
    _set(monkeypatch, **YOUTUBE_CREDS)
    monkeypatch.setattr(integrations, "egress_policy_refusal", lambda url, **kwargs: None)
    _patch_httpx_stream(monkeypatch, fault=httpx.ConnectError("no route to media host w8"))
    upload_calls = _patch_httpx_post(monkeypatch, [_FakeResponse(200, {"id": "yt-w8"})])

    out = integrations.post_tweet("hi w8", platform="youtube", image_url=MEDIA_URL)

    assert out["ok"] is False
    assert "outcome_unknown" not in out["error"]["context"]
    assert "no route to media host w8" in out["error"]["message"]
    assert upload_calls == [], "no upload may be attempted when the media never arrived"


def test_youtube_upload_no_answer_is_the_named_unknown_outcome(monkeypatch):
    _set(monkeypatch, **YOUTUBE_CREDS)
    monkeypatch.setattr(integrations, "egress_policy_refusal", lambda url, **kwargs: None)
    media_calls = _patch_httpx_stream(monkeypatch)
    _patch_httpx_post(monkeypatch, [httpx.ReadTimeout("upload read timed out w8")])

    out = integrations.post_tweet("hi w8", platform="youtube", image_url=MEDIA_URL)

    assert out["ok"] is False
    assert out["error"]["context"]["outcome_unknown"] is True
    assert "YouTube produced no answer within 120s" in out["error"]["message"]
    assert media_calls[0]["timeout"] == integrations.YOUTUBE_MEDIA_TIMEOUT_SECONDS == 60.0


# ─────────────────────────────────────────────────────────────────────────────
# B. PyGithub pull request: no answer -> ambiguity; bounded + no SDK auto-retry
# ─────────────────────────────────────────────────────────────────────────────

def _install_github(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> None:
    fake = types.ModuleType("github")
    fake.Github = client_cls  # type: ignore[attr-defined]

    class _GithubException(Exception):
        def __init__(self, status: int, data: Any = None) -> None:
            super().__init__(f"{status}: {data}")
            self.status = status
            self.data = data

    fake.GithubException = _GithubException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "github", fake)


def _install_tweepy(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> None:
    """A fake `tweepy` PACKAGE: `tweepy.Client` plus `tweepy.errors.HTTPException`."""
    errors = types.ModuleType("tweepy.errors")

    class _HTTPException(Exception):
        def __init__(self, response: Any) -> None:
            super().__init__(f"{response.status_code}: {response.text}")
            self.response = response

    errors.HTTPException = _HTTPException  # type: ignore[attr-defined]
    fake = types.ModuleType("tweepy")
    fake.Client = client_cls  # type: ignore[attr-defined]
    fake.errors = errors  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tweepy", fake)
    monkeypatch.setitem(sys.modules, "tweepy.errors", errors)


def test_pull_request_no_answer_is_the_named_unknown_outcome(monkeypatch):
    """RED before wave 8: a PyGithub transport fault was flattened into "failed"."""
    _set(monkeypatch, github_token="w8-gh-token")
    seen: dict[str, Any] = {}

    class _Repository:
        def create_pull(self, **kwargs: Any) -> Any:
            raise requests.ConnectionError("connection reset w8")

    class _Github:
        def __init__(self, token: str, **kwargs: Any) -> None:
            seen.update({"token": token, **kwargs})
            self.token = token

        def get_repo(self, repo: str) -> _Repository:
            return _Repository()

    _install_github(monkeypatch, _Github)
    out = integrations.open_pull_request(
        repo="acme/site", branch="w8", title="t", body="b"
    )

    assert out["ok"] is False
    error = out["error"]
    assert error["code"] == E_INTEGRATION
    assert error["context"]["outcome_unknown"] is True
    assert "GitHub produced no answer within 20s" in error["message"]
    assert "may have reached the provider" in error["message"]
    # Bounded call + NO internal urllib3 retry (a POST retry can open two PRs).
    assert seen["timeout"] == int(GITHUB_TIMEOUT_S) == 20
    assert seen["retry"] == 0


def test_a_pygithub_http_answer_is_not_an_unknown_outcome(monkeypatch):
    _set(monkeypatch, github_token="w8-gh-token")

    class _GithubAnswer(Exception):
        """`github.GithubException`'s shape: the provider ANSWERED, with a status."""

        def __init__(self, status: int, data: Any = None) -> None:
            super().__init__(f"{status}: {data}")
            self.status = status
            self.data = data

    class _Repository:
        def create_pull(self, **kwargs: Any) -> Any:
            raise _GithubAnswer(422, {"message": "Validation Failed w8"})

    class _Github:
        def __init__(self, token: str, **kwargs: Any) -> None:
            pass

        def get_repo(self, repo: str) -> _Repository:
            return _Repository()

    _install_github(monkeypatch, _Github)
    out = integrations.open_pull_request(repo="acme/site", branch="w8", title="t", body="b")

    assert out["ok"] is False
    assert "outcome_unknown" not in out["error"]["context"]
    assert "422" in out["error"]["message"]


# ─────────────────────────────────────────────────────────────────────────────
# C. Stripe refund: no answer -> ambiguity; bounded client + zero SDK network retries
# ─────────────────────────────────────────────────────────────────────────────

class _StripeError(Exception):
    """The shape stripe-python raises: `http_status` is None for `APIConnectionError`."""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class _RequestsClient:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout


def _install_stripe(monkeypatch: pytest.MonkeyPatch, *, raises: BaseException | None) -> dict:
    """A fake `stripe` module: records the client + params, then behaves as told."""
    seen: dict[str, Any] = {}

    class _Refunds:
        def create(self, params: dict) -> Any:
            seen["params"] = dict(params)
            if raises is not None:
                raise raises
            return {"id": "re_w8", "status": "succeeded"}

    class _StripeClient:
        def __init__(self, api_key: str, **kwargs: Any) -> None:
            seen.update({"api_key": api_key, **kwargs})
            self.refunds = _Refunds()

    fake = types.ModuleType("stripe")
    fake.StripeClient = _StripeClient  # type: ignore[attr-defined]
    fake.RequestsClient = _RequestsClient  # type: ignore[attr-defined]
    fake.StripeError = _StripeError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "stripe", fake)
    return seen


def test_refund_no_answer_is_the_named_unknown_outcome(monkeypatch):
    """RED before wave 8: a refund transport fault was flattened into "failed" (money!)."""
    _set(monkeypatch, stripe_secret_key="sk_test_w8_not_real")
    seen = _install_stripe(
        monkeypatch, raises=_StripeError("connection refused w8", http_status=None)
    )

    out = integrations.refund_payment(payment_id="pi_w8_refund_1", amount_cents=2500)

    assert out["ok"] is False
    error = out["error"]
    assert error["code"] == E_INTEGRATION
    assert error["context"]["outcome_unknown"] is True
    assert "Stripe produced no answer within 20s" in error["message"]
    assert "may have reached the provider" in error["message"]
    assert "sendgrid_id" not in json.dumps(out)
    assert seen["params"] == {"payment_intent": "pi_w8_refund_1", "amount": 2500}
    # The SDK's own 80 s / 2-retry defaults are NOT inherited: this adapter owns both.
    assert seen["max_network_retries"] == 0
    assert seen["http_client"].timeout == integrations.STRIPE_TIMEOUT_SECONDS == 20.0


def test_a_stripe_http_answer_is_not_an_unknown_outcome(monkeypatch):
    _set(monkeypatch, stripe_secret_key="sk_test_w8_not_real")
    _install_stripe(monkeypatch, raises=_StripeError("card declined w8", http_status=402))

    out = integrations.refund_payment(payment_id="pi_w8_refund_2")

    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert "outcome_unknown" not in out["error"]["context"]
    # The provider's own answer text is kept (never re-worded into an ambiguity claim).
    assert "card declined w8" in out["error"]["message"]
    assert out["error"]["context"]["module"] == "stripe"


# ─────────────────────────────────────────────────────────────────────────────
# D. Pinned-SDK contract tests: an SDK bump fails HERE, not in production
# ─────────────────────────────────────────────────────────────────────────────

def test_the_pinned_tweepy_client_really_has_no_timeout_parameter():
    """The session bound is the ONLY lever: prove the installed client offers none of its own."""
    import tweepy

    assert "timeout" not in inspect.signature(tweepy.Client.__init__).parameters
    source = inspect.getsource(tweepy.Client.request)
    assert "timeout" not in source, "tweepy now supports a timeout: use it instead of the session"


def test_the_pinned_pygithub_sdk_really_defaults_to_retrying_post_and_accepts_zero():
    """PyGithub's default Retry includes POST (urllib3 default does not) — measure BOTH."""
    from github import Github

    default_client = Github("ghp_w8_contract_no_network")
    default_retry = default_client._Github__requester._Requester__retry
    assert default_retry.total == 10
    assert "POST" in default_retry.allowed_methods

    bounded = Github("ghp_w8_contract_no_network", timeout=20, retry=0)
    assert bounded._Github__requester._Requester__timeout == 20
    assert bounded._Github__requester._Requester__retry == 0


def test_the_pinned_stripe_sdk_really_defaults_to_retries_and_accepts_the_bounded_knobs():
    import stripe

    # The SDK defaults this adapter overrides (measured on the installed 11.3.0).
    assert stripe.max_network_retries == 2
    assert stripe.RequestsClient()._timeout == 80

    client = stripe.StripeClient(
        "sk_test_w8_contract_no_network",
        http_client=stripe.RequestsClient(timeout=integrations.STRIPE_TIMEOUT_SECONDS),
        max_network_retries=0,
    )
    assert client._requestor._options.max_network_retries == 0
    assert client._requestor._client._timeout == 20.0


# ─────────────────────────────────────────────────────────────────────────────
# E. The error matrix: named code -> HTTP status, through the ONE real handler
# ─────────────────────────────────────────────────────────────────────────────

MATRIX = [
    (E_AUTH, 401),
    (E_CONFIG, 503),
    (E_CLI_TIMEOUT, 504),
    (E_INTEGRATION, 502),
    (E_UPSTREAM, 502),
]


def test_the_status_map_has_one_definition_and_the_provider_classes_are_named():
    from app.api.deps import STATUS_BY_CODE

    assert STATUS_BY_CODE is HTTP_STATUS_BY_CODE, "a second status table would be able to drift"
    for code, status in MATRIX:
        assert http_status_for(code) == status
    assert http_status_for("E_NOT_A_CODE") == 500  # unlisted = server fault, never a fake 200


async def test_the_real_polsia_error_handler_answers_each_provider_class_with_its_status():
    from starlette.requests import Request

    from app.main import app as application

    handler = application.exception_handlers[PolsiaError]
    scope = {
        "type": "http", "method": "POST", "path": "/w8-matrix", "headers": [],
        "query_string": b"", "scheme": "http", "server": ("testserver", 80),
        "client": ("testclient", 1),
    }
    for code, status in MATRIX:
        response = await handler(
            Request(scope), PolsiaError(code, "boom w8", how_to_fix="fix w8")
        )
        assert response.status_code == status
        body = json.loads(response.body)
        assert body["ok"] is False
        assert body["error"]["code"] == code
        assert body["error"]["message"] == "boom w8"
        assert body["error"]["how_to_fix"] == "fix w8"
        assert response.headers["X-Meridian-Error-Code"] == code
        if code == E_AUTH:
            assert response.headers["WWW-Authenticate"] == "X-API-Key"
        else:
            assert "WWW-Authenticate" not in response.headers


# ─────────────────────────────────────────────────────────────────────────────
# F. DeepSeek: the per-call bound is min(run deadline, provider setting)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("requested", "configured", "expected"),
    [
        (30, 300, 30),      # the run asked for less than the provider-wide setting
        (300, 30, 30),      # the setting caps a longer run deadline
        (300, 300, 300),    # the documented shipped run: unchanged
        (0, 300, 300),      # no deadline asked -> the setting (never "unbounded")
        (-5, 300, 300),     # a non-positive deadline is not a licence for no bound
    ],
)
def test_the_deepseek_call_timeout_is_the_smaller_of_the_run_deadline_and_the_setting(
    monkeypatch, requested, configured, expected
):
    monkeypatch.setattr(app_config.settings, "deepseek_timeout_seconds", configured)
    invocation = claude_cli.build_invocation(
        prompt="w8", json_schema=None, system_prompt_static="", resume_session=None,
        timeout_s=requested, schema_name="w8",
    )
    assert deepseek_runtime._call_timeout_seconds(invocation) == expected


def test_the_deepseek_auth_and_balance_guidance_is_scoped_to_the_provider_that_answered():
    """The CLI remedy must never be handed to a DeepSeek key failure (and vice versa)."""
    for status in (401, 403):
        hint = deepseek_runtime.failure_guidance(
            provider="deepseek", subtype="error_during_execution", api_status=status
        )
        assert hint is not None and "DeepSeek rejected the API key" in hint
        for cli_phrase in ("claude login", "CLAUDE_CONFIG_DIR", "not a supported fix"):
            assert cli_phrase not in hint
        assert deepseek_runtime.operator_action_required(
            provider="deepseek", subtype="error_during_execution", api_status=status
        ) is True

    balance = deepseek_runtime.failure_guidance(
        provider="deepseek", subtype="error_insufficient_balance", api_status=402
    )
    assert balance is not None and "insufficient balance" in balance and "top up" in balance

    # A non-DeepSeek envelope gets NO overlay: the CLI's own remediation stays byte-identical.
    assert deepseek_runtime.failure_guidance(
        provider="", subtype="error_during_execution", api_status=401
    ) is None
    assert deepseek_runtime.operator_action_required(
        provider="", subtype="error_during_execution", api_status=401
    ) is False
