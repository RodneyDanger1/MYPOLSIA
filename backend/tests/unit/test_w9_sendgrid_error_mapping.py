"""WAVE 9 / FORGE — the SendGrid Mail Send ANSWER mapping, the anchored dedupe fallback, and the
provider-matrix pins.

WHAT WAS WRONG BEFORE THIS FILE (measured on HEAD `0a25d45`, not inferred)

  A. The Mail Send page documents exactly `202` plus `400/401/403/404/405/413/500`, each error
     answer carrying `{"errors": [{"field", "message", "help"}], "id"}`.  The adapter collapsed
     EVERY one of them into the pinned SDK's own `str(exc)` inside ONE `E_INTEGRATION`, so a 413
     (the body is too large), a 401 (the key is wrong) and a 500 (SendGrid is the failing side,
     retry) were indistinguishable — and the retry decision cannot be made from an
     indistinguishable refusal.  This file pins the classification, the parsed error surface and
     the retryable verdict, while proving the ONE envelope and its `outcome_unknown` boundary.
  B. `X-Message-Id` is the dedupe key (`email_logs.sendgrid_id`) and appears ZERO times on the
     vendor's Mail Send page; the old fallback was `""`, which made an accepted send
     unrecordable ("failed") and left no trace at the provider.  This file pins the ANCHORED
     fallback: a correlation id minted before the request and carried in the documented
     `custom_args` field, `message_id` never invented, and the absence LOGGED.
  C. The classification must never leak into the no-answer class (a transport fault is still
     `outcome_unknown`, and never carries a provider status verdict).

Doubles live ONLY at the third-party edge (a fake `sendgrid` module package).  The pinned REAL
SDK is also built/instantiated once so an SDK bump fails a contract test HERE instead of silently
re-pinning an assumption (the same discipline as the wave-7 timeout-lever pin).
"""
from __future__ import annotations

import json
import logging
import sys
import types
from typing import Any

import pytest

import app.config as app_config
from app.agents import integrations
from app.core.errors import E_INTEGRATION

SENDGRID_KEY = "SG.w9-not-a-real-key"
FROM_EMAIL = "ops@example.com"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """The credential gate is satisfied; the provider module is a double per test."""
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", SENDGRID_KEY)
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", FROM_EMAIL)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# The third-party edge (sendgrid 6.11 + python_http_client 3.3.7 shapes)
# ─────────────────────────────────────────────────────────────────────────────

class _HttpAnswer(Exception):
    """`python_http_client.exceptions.HTTPError` shape: a status, a reason, a BODY, headers."""

    def __init__(self, status_code: int, body: Any = b"", reason: str = "Provider Error") -> None:
        super().__init__(f"HTTP Error {status_code}: {reason}")
        self.status_code = status_code
        self.body = body
        self.reason = reason
        self.headers = {}


class _FakeResponse:
    def __init__(self, status_code: int = 202, headers: dict[str, str] | None = None,
                 body: Any = b"") -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body


class _FakeCustomArg:
    def __init__(self, key: str | None = None, value: str | None = None) -> None:
        self.key = key
        self.value = value


class _FakeMail:
    """The sendgrid `Mail` surface this adapter uses, plus a record of the custom args."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.custom_args: dict[str, str] = {}
        self.headers: dict[str, str] = {}

    def add_custom_arg(self, custom_arg: Any) -> None:
        if isinstance(custom_arg, dict):
            self.custom_args.update(custom_arg)
        else:
            self.custom_args[custom_arg.key] = custom_arg.value


def _install_sendgrid(monkeypatch, *, response: Any = None, raises: BaseException | None = None,
                      mail_cls: Any = _FakeMail) -> dict[str, Any]:
    """A fake `sendgrid` package; returns the recorder (`message` is the sent Mail object)."""
    seen: dict[str, Any] = {}

    class _Client:
        def __init__(self, api_key: str) -> None:
            seen["api_key"] = api_key
            self.client = types.SimpleNamespace(timeout=None)

        def send(self, message: Any) -> Any:
            seen["message"] = message
            if raises is not None:
                raise raises
            return response

    sendgrid_mod = types.ModuleType("sendgrid")
    helpers = types.ModuleType("sendgrid.helpers")
    mail_mod = types.ModuleType("sendgrid.helpers.mail")
    sendgrid_mod.SendGridAPIClient = _Client  # type: ignore[attr-defined]
    sendgrid_mod.helpers = helpers  # type: ignore[attr-defined]
    helpers.mail = mail_mod  # type: ignore[attr-defined]
    mail_mod.Mail = mail_cls  # type: ignore[attr-defined]
    mail_mod.CustomArg = _FakeCustomArg  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sendgrid", sendgrid_mod)
    monkeypatch.setitem(sys.modules, "sendgrid.helpers", helpers)
    monkeypatch.setitem(sys.modules, "sendgrid.helpers.mail", mail_mod)
    return seen


def _documented_body(*, field: str = "content", message: str = "The message is too large.",
                     help_text: str = "reduce the message size", request_id: str = "req-413") -> bytes:
    """The DOCUMENTED error body: `{"errors": [{field,message,help}], "id"}`."""
    return json.dumps({
        "errors": [{"field": field, "message": message, "help": help_text}],
        "id": request_id,
    }).encode()


def _send(**overrides: Any) -> dict:
    kwargs: dict[str, Any] = {"to": "ada@example.com", "subject": "hello", "body": "the body"}
    kwargs.update(overrides)
    return integrations.send_email(**kwargs)


def _message(out: dict) -> str:
    return str(out["error"]["message"])


# ─────────────────────────────────────────────────────────────────────────────
# A. The documented answer table -> a named class + a retryable verdict
# ─────────────────────────────────────────────────────────────────────────────

#: The vendor page's list, with the class/retry verdict this adapter must derive from it.
DOCUMENTED = [
    (400, "caller_error", False),
    (401, "caller_error", False),
    (403, "caller_error", False),
    (404, "caller_error", False),
    (405, "caller_error", False),
    (413, "payload_too_large", False),
    (500, "upstream_error", True),
]


def test_the_documented_table_is_the_vendor_page_and_the_extension_is_separate():
    """Pin the table to the page's exact status list (an SDK bump cannot silently widen it)."""
    assert set(integrations.SENDGRID_ERROR_CLASSES) == {400, 401, 403, 404, 405, 413, 500}
    assert {status: klass for status, (klass, _r) in integrations.SENDGRID_ERROR_CLASSES.items()} == {
        status: klass for status, klass, _r in DOCUMENTED
    }
    assert {status: r for status, (_k, r) in integrations.SENDGRID_ERROR_CLASSES.items()} == {
        status: r for status, _k, r in DOCUMENTED
    }
    # The page does not list these; the pinned SDK raises them, so they must still be classed.
    assert set(integrations.SENDGRID_UNDOCUMENTED_STATUS_CLASSES) == {429, 503, 504}
    assert set(integrations.SENDGRID_ERROR_CLASSES).isdisjoint(
        integrations.SENDGRID_UNDOCUMENTED_STATUS_CLASSES
    )
    assert integrations.SENDGRID_ACCEPTED_STATUSES == (200, 201, 202)


@pytest.mark.parametrize(("status", "klass", "retryable"), DOCUMENTED)
def test_each_documented_status_is_named_with_its_class_and_retry_verdict(
    monkeypatch, status, klass, retryable
):
    """RED before wave 9: every status was the same flattened string (no class, no verdict)."""
    body = _documented_body(request_id=f"req-{status}")
    _install_sendgrid(
        monkeypatch, raises=_HttpAnswer(status, body, reason=f"provider says {status}")
    )

    out = _send()

    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    message = _message(out)
    assert f"HTTP {status}" in message
    assert klass in message, message
    assert f"retryable={'true' if retryable else 'false'}" in message, message
    # An ANSWER is a definite "no": it must never claim the ambiguity (that is the no-answer class).
    assert "outcome_unknown" not in out["error"]["context"]


def test_a_413_is_a_payload_problem_and_a_401_is_a_caller_problem_and_a_500_is_upstream(
    monkeypatch,
):
    """The three classes the old refusal could not tell apart, in one test."""
    messages: dict[int, str] = {}
    for status in (413, 401, 500):
        _install_sendgrid(
            monkeypatch,
            raises=_HttpAnswer(status, _documented_body(request_id=f"req-{status}")),
        )
        messages[status] = _message(_send())

    assert "payload_too_large" in messages[413]
    assert "caller_error" in messages[401]
    assert "upstream_error" in messages[500]
    # And the retry verdicts differ in the direction an operator needs.
    assert "retryable=false" in messages[413]
    assert "retryable=false" in messages[401]
    assert "retryable=true" in messages[500]
    assert len({messages[413], messages[401], messages[500]}) == 3


@pytest.mark.parametrize("status", [429, 503, 504])
def test_the_sdk_classes_the_page_does_not_list_are_classified_not_flattened(monkeypatch, status):
    _install_sendgrid(monkeypatch, raises=_HttpAnswer(status, _documented_body(request_id="r")))

    out = _send()

    assert out["ok"] is False
    message = _message(out)
    assert f"HTTP {status}" in message
    assert "unclassified" not in message
    assert "retryable=true" in message


def test_an_unlisted_4xx_is_caller_error_and_an_unlisted_5xx_is_upstream(monkeypatch):
    _install_sendgrid(monkeypatch, raises=_HttpAnswer(418, b""))
    assert "caller_error" in _message(_send())

    _install_sendgrid(monkeypatch, raises=_HttpAnswer(599, b""))
    assert "upstream_error" in _message(_send())


# ─────────────────────────────────────────────────────────────────────────────
# B. The documented error BODY is parsed, bounded, and never invented
# ─────────────────────────────────────────────────────────────────────────────

def test_the_documented_errors_and_request_id_reach_the_refusal(monkeypatch):
    """RED before wave 9: `errors[]`/`id` were never parsed — only `str(exc)` was kept."""
    body = _documented_body(
        field="personalizations.0.to.0.email",
        message="Does not contain a valid address.",
        help_text="provide a valid email address",
        request_id="req-body-id",
    )
    _install_sendgrid(monkeypatch, raises=_HttpAnswer(400, body))

    out = _send()
    message = _message(out)

    assert "field='personalizations.0.to.0.email'" in message
    assert "message='Does not contain a valid address.'" in message
    assert "help='provide a valid email address'" in message
    assert "provider request id: req-body-id" in message


@pytest.mark.parametrize("body", [b"not json at all", b"", b"[]", None, "plain text"])
def test_a_body_that_is_not_the_documented_json_is_refused_without_invention(monkeypatch, body):
    _install_sendgrid(monkeypatch, raises=_HttpAnswer(400, body))

    out = _send()
    message = _message(out)

    assert out["ok"] is False
    assert "HTTP 400" in message
    # No invented request id, and no crash on a malformed body.
    assert "provider request id" not in message


def test_a_str_body_with_the_documented_shape_is_parsed_too(monkeypatch):
    body = json.dumps({"errors": [{"field": "f", "message": "m", "help": "h"}], "id": "req-str"})
    _install_sendgrid(monkeypatch, raises=_HttpAnswer(413, body))

    message = _message(_send())

    assert "field='f'" in message
    assert "provider request id: req-str" in message


def test_the_parsed_errors_are_bounded(monkeypatch):
    """A provider response is attacker-adjacent input: the refusal is not a log dump."""
    body = json.dumps({
        "errors": [
            {"field": "f" * 900, "message": "m" * 900, "help": "h" * 900}
            for _ in range(20)
        ],
        "id": "i" * 900,
    }).encode()
    _install_sendgrid(monkeypatch, raises=_HttpAnswer(400, body))

    message = _message(_send())

    assert message.count("field=") <= 5
    assert "f" * 201 not in message
    assert "i" * 201 not in message


# ─────────────────────────────────────────────────────────────────────────────
# C. The ONE envelope, and the `outcome_unknown` boundary in BOTH directions
# ─────────────────────────────────────────────────────────────────────────────

def test_every_answer_keeps_the_one_envelope(monkeypatch):
    for status in (400, 401, 403, 404, 405, 413, 500):
        _install_sendgrid(monkeypatch, raises=_HttpAnswer(status, _documented_body()))
        out = _send()

        assert set(out) == {"ok", "error"}
        assert out["ok"] is False
        error = out["error"]
        assert error["code"] == E_INTEGRATION
        assert error["message"]
        assert error["how_to_fix"]
        assert error["context"]["module"] == "sendgrid"
        assert error["context"]["attribute"] == "SendGridAPIClient"


def test_a_no_answer_fault_stays_the_ambiguity_class_and_is_never_an_answer(monkeypatch):
    """The other direction: a transport fault is NOT a provider answer with a status verdict."""
    _install_sendgrid(monkeypatch, raises=TimeoutError("timed out after 20s"))

    out = _send()
    error = out["error"]

    assert out["ok"] is False
    assert error["context"]["outcome_unknown"] is True
    assert "no answer" in error["message"]
    assert "may have reached the provider" in error["message"]
    # No classification verdict is invented for a request that produced no answer.
    assert "retryable=" not in error["message"]
    assert "payload_too_large" not in error["message"]
    assert "provider request id" not in error["message"]


def test_an_error_status_RETURNED_instead_of_raised_is_classified_and_never_a_send(monkeypatch):
    seen = _install_sendgrid(
        monkeypatch,
        response=_FakeResponse(413, body=_documented_body(request_id="req-returned")),
    )

    out = _send()

    assert seen["message"] is not None
    assert out["ok"] is False
    message = _message(out)
    assert "HTTP 413" in message
    assert "payload_too_large" in message
    assert "retryable=false" in message
    assert "provider request id: req-returned" in message
    assert "outcome_unknown" not in out["error"]["context"]


# ─────────────────────────────────────────────────────────────────────────────
# D. X-Message-Id: the anchored fallback, and no fabricated id
# ─────────────────────────────────────────────────────────────────────────────

def test_a_202_with_the_header_returns_the_provider_id_and_its_source(monkeypatch):
    seen = _install_sendgrid(
        monkeypatch, response=_FakeResponse(202, {"X-Message-Id": "sg-provider-id"})
    )

    out = _send()

    assert out["ok"] is True
    result = out["result"]
    assert result["status_code"] == 202
    assert result["message_id"] == "sg-provider-id"
    assert result["message_id_source"] == integrations.SENDGRID_ID_SOURCE_HEADER
    assert result["correlation_id"].startswith(integrations.SENDGRID_CORRELATION_PREFIX)
    # The anchor really travelled with the message (the documented `custom_args` body field).
    assert seen["message"].custom_args == {
        integrations.SENDGRID_CORRELATION_ARG: result["correlation_id"]
    }


def test_a_202_without_the_header_is_anchored_not_fabricated(monkeypatch, caplog):
    """RED before wave 9: the fallback was `""` — no anchor, and an accepted send unrecordable."""
    _install_sendgrid(monkeypatch, response=_FakeResponse(202, {}))

    with caplog.at_level(logging.WARNING, logger="app.agents.integrations"):
        out = _send()

    assert out["ok"] is True
    result = out["result"]
    assert result["message_id"] == "", "a provider id is never invented"
    assert result["message_id_source"] == integrations.SENDGRID_ID_SOURCE_ABSENT
    correlation = result["correlation_id"]
    assert correlation.startswith(integrations.SENDGRID_CORRELATION_PREFIX)
    assert len(correlation) == len(integrations.SENDGRID_CORRELATION_PREFIX) + 32
    # The status code is NOT the dedupe key (the RD14 measurement: three sends collapsed to one).
    assert result["message_id"] != str(result["status_code"])
    # The absence is OBSERVABLE, never silent.
    assert any("X-Message-Id" in record.getMessage() for record in caplog.records)


def test_two_sends_never_share_a_correlation_id(monkeypatch):
    _install_sendgrid(monkeypatch, response=_FakeResponse(202, {}))
    first = _send()["result"]["correlation_id"]
    _install_sendgrid(monkeypatch, response=_FakeResponse(202, {}))
    second = _send()["result"]["correlation_id"]

    assert first != second


def test_an_unconfigured_provider_refuses_before_any_send(monkeypatch):
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "")
    _install_sendgrid(monkeypatch, response=_FakeResponse(202, {}))

    out = _send()

    assert out["ok"] is False
    assert "configuration missing" in out["error"]["message"]
    assert "SENDGRID_API_KEY" in out["error"]["message"]


# ─────────────────────────────────────────────────────────────────────────────
# E. Pinned-SDK contract tests: an SDK bump fails HERE, not in production
# ─────────────────────────────────────────────────────────────────────────────

def test_the_pinned_sdk_really_carries_a_message_level_custom_arg():
    """The anchor rides `custom_args`; prove the REAL SDK serializes it (no network)."""
    from sendgrid.helpers.mail import CustomArg, Mail

    message = Mail(from_email=FROM_EMAIL, to_emails="ada@example.com", subject="s",
                   plain_text_content="b")
    assert callable(getattr(message, "add_custom_arg", None)), (
        "the pinned SDK no longer exposes Mail.add_custom_arg: the X-Message-Id fallback would "
        "have no anchor — re-measure the adapter"
    )
    message.add_custom_arg(CustomArg(integrations.SENDGRID_CORRELATION_ARG, "polsia-contract"))
    body = message.get()
    assert body["custom_args"] == {integrations.SENDGRID_CORRELATION_ARG: "polsia-contract"}


def test_the_pinned_sdk_error_really_carries_status_and_the_documented_body():
    """The classification reads `.status_code` and `.body`; prove those on the REAL exception."""
    from python_http_client.exceptions import HTTPError

    error = HTTPError(413, "Payload Too Large", _documented_body(request_id="req-sdk"), {})

    assert getattr(error, "status_code", None) == 413
    errors, request_id = integrations._sendgrid_answer_fields(getattr(error, "body", None))
    assert errors and errors[0]["message"] == "The message is too large."
    assert request_id == "req-sdk"
    klass, retryable = integrations._sendgrid_error_class(error.status_code)
    assert (klass, retryable) == ("payload_too_large", False)


def test_the_real_sdk_classes_are_in_the_extension_table():
    """The undocumented table exists because the SDK raises typed classes for them."""
    from python_http_client import exceptions as phc

    for class_name, status in (
        ("PayloadTooLargeError", 413),
        ("UnauthorizedError", 401),
        ("InternalServerError", 500),
    ):
        assert hasattr(phc, class_name), f"pinned SDK no longer models {class_name}"
        assert integrations._sendgrid_error_class(status)[0] != "unclassified"


# ─────────────────────────────────────────────────────────────────────────────
# F. Doc truth (SV-W8-05): the 403 claim the vendor does not make
# ─────────────────────────────────────────────────────────────────────────────

def test_the_deepseek_runtime_no_longer_claims_403_is_documented():
    """Comment-only repair: the vendor documents 401 and 402; 403 is defensive, behaviour kept."""
    from app.agents import deepseek_runtime

    source = (
        __import__("pathlib").Path(deepseek_runtime.__file__).read_text(encoding="utf-8")
    )
    assert "Both are documented DeepSeek statuses" not in source
    assert "does NOT list 403" in source
    # The behaviour is unchanged: 401/403 stay the auth family, 402 the balance family.
    assert deepseek_runtime.AUTH_REJECTED_STATUSES == (401, 403)
    assert deepseek_runtime.BALANCE_REJECTED_STATUSES == (402,)
