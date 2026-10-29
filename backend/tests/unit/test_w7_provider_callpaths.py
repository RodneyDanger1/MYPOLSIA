"""WAVE 7 / FORGE — the provider call paths: DeepSeek auth mapping, retry-ladder agreement,
and the SendGrid send boundary.

Every test here proves a behaviour that was WRONG or UNPROVEN before wave 7:

  A. a DeepSeek 401/403 was reported with the CLI's remediation ("run `claude login` … an API
     key is NOT a supported fix") — the opposite of the truth for the runtime that answered —
     and a 402 fell through to a generic sentence that never named the account's balance;
  B. the task-level requeue ladder classified those same failures RETRYABLE (they share the
     generic `E_CLI_EXIT` code with transient exits), so a rejected key was redelivered
     `agent_max_retries` times, every attempt doomed by construction;
  C. the SendGrid send had NO timeout (the SDK ships none: `python_http_client.Client(timeout=None)`)
     and a transport fault was flattened into the same "failed" as a provider answer, inviting a
     blind re-send of a message that may already have been delivered;
  D. a response that carried an error status instead of raising was accepted as a success.

Doubles live ONLY at the third-party edges (the sendgrid SDK module, `httpx.MockTransport` as the
DeepSeek HTTP seam) — the code under test is the real adapter, the real runtime and the real
classifiers.
"""
from __future__ import annotations

import json
import sys
import types
from typing import Any

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime, integrations, structured_output
from app.agents.base_agent import BaseMeridianAgent
from app.config import settings as app_settings
from app.core.errors import E_CLI_EXIT, E_INTEGRATION, MeridianError

SAFE_TEST_KEY = "sk-w7-unit-not-a-real-key"


class _Agent(BaseMeridianAgent):
    """The smallest real agent: `_finish_call` is the code under test, nothing is dispatched."""

    agent_type = "test"

    def run(self, task, context):  # pragma: no cover - never dispatched here
        return {"summary": "test"}


def _inv() -> claude_cli.Invocation:
    return claude_cli.build_invocation(
        prompt="do the thing", json_schema=None, system_prompt_static="",
        resume_session=None, timeout_s=30, schema_name="test",
    )


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _status_envelope(status: int, body: dict | None = None) -> claude_cli.InvokeResult:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body or {"error": {"message": f"provider {status}"}})

    return deepseek_runtime.invoke(_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)


def _raised_failure(result: claude_cli.InvokeResult, attempts: int = 1) -> MeridianError:
    with pytest.raises(MeridianError) as excinfo:
        _Agent()._finish_call(result, attempts=attempts, transport="http")
    return excinfo.value


def _worker_retryable(exc: BaseException | None) -> bool:
    from celery_app.tasks.agent_tasks import is_retryable

    return is_retryable(exc)


# ─────────────────────────────────────────────────────────────────────────────
# A. DeepSeek auth / balance mapping — the guidance names the provider that answered
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_names_the_deepseek_key_and_never_the_cli_login(status):
    """RED before wave 7: the hint told the operator to run `claude login` and said an API key
    was not supported — for a runtime whose ONLY credential IS the API key."""
    exc = _raised_failure(_status_envelope(status))

    assert exc.code == E_CLI_EXIT  # taxonomy unchanged (the API boundary still answers 502)
    assert exc.context["failure_code"] == "MERIDIAN-E101"
    assert exc.context["provider"] == "deepseek"
    assert exc.context["api_error_status"] == status
    assert "DeepSeek rejected the API key" in exc.how_to_fix
    assert f"HTTP {status}" in exc.how_to_fix
    # The CLI's spawn-path text must not appear at all: it is the WRONG diagnosis here.
    for cli_phrase in ("claude login", "~/.claude", "CLAUDE_CONFIG_DIR", "not a supported fix"):
        assert cli_phrase not in exc.how_to_fix
    # 401/403 are an operator action, not a transient condition: the verdict rides the refusal.
    assert exc.context["retryable"] is False


def test_insufficient_balance_is_named_as_the_account_condition_it_is():
    """RED before wave 7: 402 fell through to "Branch on subtype/api_error_status" — no balance."""
    exc = _raised_failure(_status_envelope(402))

    assert exc.context["failure_code"] == "MERIDIAN-E105"
    assert exc.context["api_error_status"] == 402
    assert "insufficient balance" in exc.how_to_fix
    assert "402" in exc.how_to_fix
    assert "top up" in exc.how_to_fix
    assert exc.context["retryable"] is False


def test_a_rate_limit_keeps_its_generic_hint_and_stays_retryable():
    """The fix must not swallow the transient class: 429 is still retryable at BOTH ladders."""
    result = _status_envelope(429)
    exc = _raised_failure(result)

    assert exc.context["failure_code"] == "MERIDIAN-E105"
    assert "retryable" not in exc.context  # no verdict -> the code table answers, as before
    assert "claude login" not in exc.how_to_fix
    assert _Agent()._retryable(result) is True
    assert _worker_retryable(exc) is True


def test_a_cli_envelope_keeps_the_cli_remediation_byte_for_byte():
    """The overlay is scoped by the envelope's `provider` stamp: a CLI failure is untouched."""
    envelope = {
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "auth", "api_error_status": 401, "session_id": "s-cli",
    }
    result = claude_cli.InvokeResult(stdout=json.dumps(envelope), stderr="", exit_code=1, wall_seconds=0.1)
    exc = _raised_failure(result)

    assert exc.how_to_fix == structured_output.REMEDIATION[structured_output.FailureCode.CLI_AUTH_FAILED]
    assert "claude login" in exc.how_to_fix
    assert "provider" not in exc.context
    assert "retryable" not in exc.context  # the CLI's task ladder keeps its shipped answer


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_the_key_never_reaches_the_refusal_or_the_envelope(status):
    result = _status_envelope(status)
    exc = _raised_failure(result)

    haystack = json.dumps(
        {"message": exc.message, "hint": exc.how_to_fix, "context": exc.context,
         "str": str(exc)},
        default=str,
    )
    assert SAFE_TEST_KEY not in haystack
    assert SAFE_TEST_KEY not in result.stdout
    assert SAFE_TEST_KEY not in result.stderr


def test_a_provider_body_that_echoes_the_key_is_redacted_before_it_becomes_the_error():
    """A hostile proxy can echo the request headers into its 401 body. The refusal must scrub it."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": f"bad key: {SAFE_TEST_KEY}"}},
        )

    result = deepseek_runtime.invoke(_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    exc = _raised_failure(result)

    assert SAFE_TEST_KEY not in result.stdout
    assert SAFE_TEST_KEY not in result.stderr
    assert SAFE_TEST_KEY not in json.dumps(
        {"m": exc.message, "h": exc.how_to_fix, "c": exc.context}, default=str
    )


# ─────────────────────────────────────────────────────────────────────────────
# B. One verdict, two ladders
# ─────────────────────────────────────────────────────────────────────────────

def test_the_task_ladder_reads_a_declared_verdict_and_falls_back_to_the_table():
    from celery_app.tasks.agent_tasks import is_retryable

    # A declared verdict wins, in both directions.
    assert is_retryable(MeridianError(E_CLI_EXIT, "x", context={"retryable": False})) is False
    assert is_retryable(MeridianError(E_CLI_EXIT, "x", context={"retryable": True})) is True
    # A malformed value is not a licence to retry: the code table answers.
    assert is_retryable(MeridianError(E_CLI_EXIT, "x", context={"retryable": "yes"})) is True
    # No verdict at all: the shipped table, byte for byte.
    assert is_retryable(MeridianError(E_CLI_EXIT, "x")) is True
    assert is_retryable(MeridianError("E_RATE_LIMITED", "x")) is False
    assert is_retryable(MeridianError("E_CLI_TIMEOUT", "hang")) is True
    assert is_retryable(None) is True
    assert is_retryable(RuntimeError("driver")) is True


def test_a_rejected_key_is_not_requeued_even_though_its_code_is_retryable():
    """RED before wave 7: `task_requeue_retryable` was True for a 401 — three doomed attempts."""
    exc = _raised_failure(_status_envelope(401))

    assert exc.code == E_CLI_EXIT  # the code IS in RETRYABLE_CODES
    from celery_app.tasks.agent_tasks import RETRYABLE_CODES

    assert E_CLI_EXIT in RETRYABLE_CODES
    assert _worker_retryable(exc) is False  # …but the declared verdict is terminal


# ─────────────────────────────────────────────────────────────────────────────
# C/D. The SendGrid send boundary
# ─────────────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _FakeSendGridClient:
    """The third-party edge (sendgrid SDK): records the client it built, then behaves as told."""

    last: _FakeSendGridClient | None = None

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.client = types.SimpleNamespace(timeout=None)  # what the adapter must bound
        self.messages: list[Any] = []
        type(self).last = self

    def send(self, message: Any) -> _FakeResponse:
        self.messages.append(message)
        raise AssertionError("test installed no send() behaviour")  # overridden per test


def _install_sendgrid(monkeypatch, client_cls) -> Any:
    """Install a fake `sendgrid` module package (the repo's own pattern for this edge)."""
    sendgrid_mod = types.ModuleType("sendgrid")
    helpers = types.ModuleType("sendgrid.helpers")
    mail_mod = types.ModuleType("sendgrid.helpers.mail")
    sendgrid_mod.SendGridAPIClient = client_cls  # type: ignore[attr-defined]
    sendgrid_mod.helpers = helpers  # type: ignore[attr-defined]
    helpers.mail = mail_mod  # type: ignore[attr-defined]
    mail_mod.Mail = lambda **kwargs: {"mail": kwargs}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sendgrid", sendgrid_mod)
    monkeypatch.setitem(sys.modules, "sendgrid.helpers", helpers)
    monkeypatch.setitem(sys.modules, "sendgrid.helpers.mail", mail_mod)
    return sendgrid_mod


@pytest.fixture
def configured_sendgrid(monkeypatch):
    monkeypatch.setattr(app_settings, "sendgrid_api_key", "SG.w7-not-a-real-key")
    monkeypatch.setattr(app_settings, "sendgrid_from_email", "ops@example.com")


def test_the_pinned_sdk_really_exposes_the_timeout_lever():
    """The adapter bounds the call through `client.client.timeout`; prove the REAL SDK has it.

    WHY this test exists: the adapter's `getattr` guard keeps a test double that models only
    `send()` usable, and a guard that silently found nothing in PRODUCTION would restore an
    unbounded send.  This builds the installed, pinned `sendgrid.SendGridAPIClient` (no network —
    the constructor only assembles the client) and asserts the attribute is there and starts as
    the documented `None`.  An SDK bump that moves it fails HERE, loudly, instead of leaving a
    send that can block a worker forever.
    """
    from sendgrid import SendGridAPIClient

    client = SendGridAPIClient("SG.w7-contract-check-no-network")
    inner = getattr(client, "client", None)
    assert inner is not None, "the pinned SDK no longer exposes .client (timeout lever moved)"
    assert inner.timeout is None, "the SDK now ships its own default; re-measure the adapter bound"
    inner.timeout = integrations.SENDGRID_TIMEOUT_SECONDS
    assert inner.timeout == 20.0


def test_the_send_is_bounded_by_the_adapter_not_left_to_the_os(monkeypatch, configured_sendgrid):
    """RED before wave 7: the SDK's own default is `timeout=None` (block until the OS gives up)."""
    class _Ok(_FakeSendGridClient):
        def send(self, message):
            self.messages.append(message)
            return _FakeResponse(202, {"X-Message-Id": "sg-w7-1"})

    _install_sendgrid(monkeypatch, _Ok)
    out = integrations.send_email(to="a@example.com", subject="s", body="b")

    assert out["ok"] is True, out
    built = _Ok.last
    assert built is not None
    assert built.client.timeout == integrations.SENDGRID_TIMEOUT_SECONDS == 20.0
    # REPAIRED PIN (wave 9, forge — SV-W8-03).  The result grew the two fields the anchored
    # `X-Message-Id` fallback needs, so an exact-equality check on the old TWO-key shape would
    # pin the pre-wave-9 behaviour:
    #   OLD: {"status_code": 202, "message_id": "sg-w7-1"}
    #   NEW: the same two values, PLUS `message_id_source` (which evidence the id is) and
    #        `correlation_id` (the anchor minted for this attempt).
    # Every old assertion still holds exactly: the status and the provider id are unchanged.
    assert out["result"]["status_code"] == 202
    assert out["result"]["message_id"] == "sg-w7-1"
    assert out["result"]["message_id_source"] == integrations.SENDGRID_ID_SOURCE_HEADER
    assert out["result"]["correlation_id"].startswith(integrations.SENDGRID_CORRELATION_PREFIX)
    assert set(out["result"]) == {
        "status_code", "message_id", "message_id_source", "correlation_id"
    }


def test_a_transport_fault_is_a_named_refusal_that_names_the_unknown_outcome(
    monkeypatch, configured_sendgrid
):
    """RED before wave 7: a timeout became a bare "failed" — a retry could double-send."""
    class _Timeout(_FakeSendGridClient):
        def send(self, message):
            raise TimeoutError("timed out after 20s")

    _install_sendgrid(monkeypatch, _Timeout)
    out = integrations.send_email(to="a@example.com", subject="s", body="b")

    assert out["ok"] is False
    error = out["error"]
    assert error["code"] == E_INTEGRATION
    assert error["context"]["outcome_unknown"] is True
    assert error["context"]["module"] == "sendgrid"
    assert "no answer" in error["message"]
    assert "may have reached the provider" in error["message"]
    assert "20" in error["message"]  # the bound is named, not just "a timeout"
    assert "sendgrid_id" not in json.dumps(out)


def test_a_provider_answer_is_not_reported_as_an_unknown_outcome(monkeypatch, configured_sendgrid):
    class _ProviderError(_FakeSendGridClient):
        def send(self, message):
            error = RuntimeError("HTTP Error 401: Unauthorized")
            error.status_code = 401  # the python_http_client shape: an ANSWER, with a status
            raise error

    _install_sendgrid(monkeypatch, _ProviderError)
    out = integrations.send_email(to="a@example.com", subject="s", body="b")

    assert out["ok"] is False
    error = out["error"]
    assert error["code"] == E_INTEGRATION
    assert "outcome_unknown" not in error["context"]
    assert "401" in error["message"]


def test_an_error_status_returned_instead_of_raised_is_never_recorded_as_a_send(
    monkeypatch, configured_sendgrid
):
    """RED before wave 7: any non-raising response was `ok: True` — a 500 could look sent."""
    class _BadStatus(_FakeSendGridClient):
        def send(self, message):
            return _FakeResponse(500, {})

    _install_sendgrid(monkeypatch, _BadStatus)
    out = integrations.send_email(to="a@example.com", subject="s", body="b")

    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert "500" in out["error"]["message"]
    assert "not an accepted send status" in out["error"]["message"]


def test_missing_credentials_refuse_before_the_provider_module_is_ever_imported(monkeypatch):
    """The credential gate is the FIRST leg: it must not touch the SDK (or the network)."""
    monkeypatch.setattr(app_settings, "sendgrid_api_key", "")
    monkeypatch.setattr(app_settings, "sendgrid_from_email", "")

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the SDK was constructed without credentials")

    _install_sendgrid(monkeypatch, _explode)
    out = integrations.send_email(to="a@example.com", subject="s", body="b")

    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert out["error"]["context"]["missing"] == ["SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL"]
