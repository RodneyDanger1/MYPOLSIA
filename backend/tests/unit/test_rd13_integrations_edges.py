"""RD13 — edge tests for the third-party action adapters (`app.agents.integrations`).

The production call path runs for real (`integration_call` lazy import -> adapter ->
provider call); only the THIRD-PARTY LIBRARY is doubled, injected through `sys.modules`
(and a `None` entry to simulate "not installed"). That matters on this host: the venv HAS
tweepy/sendgrid/PyGithub/stripe installed, so without the doubles a credentials-present
adapter would attempt a REAL provider call from the unit suite — which must never happen
here.

Read-verified on this tree: no adapter has another code path to pin — each refuses by name
(`E_INTEGRATION` for unconfigured/uninstalled/provider-failed, `E_VALIDATION` for a
platform the adapter does not handle), and `update_ad_budget` refuses by design (no
account-scoped client is shipped; the recommendation is recorded instead).
"""
from __future__ import annotations

import sys
import types

import pytest

from app.agents import integrations
from app.config import settings
from app.core.errors import E_INTEGRATION, E_VALIDATION

TWITTER_FIELDS = (
    "twitter_api_key", "twitter_api_secret", "twitter_access_token", "twitter_access_secret",
)
GOOGLE_ADS_FIELDS = (
    "google_ads_developer_token", "google_ads_client_id",
    "google_ads_refresh_token", "google_ads_customer_id",
)
META_ADS_FIELDS = ("meta_ads_access_token", "meta_ads_account_id")


def _set(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for field, value in values.items():
        monkeypatch.setattr(settings, field, value)


def _clear(monkeypatch: pytest.MonkeyPatch, *fields: str) -> None:
    for field in fields:
        monkeypatch.setattr(settings, field, "")


# ── post_tweet ───────────────────────────────────────────────────────────────────────────────


def test_post_tweet_refuses_an_unsupported_platform_by_name() -> None:
    result = integrations.post_tweet("hello", platform="mastodon")
    assert result["ok"] is False
    assert result["error"]["code"] == E_VALIDATION
    assert "mastodon" in result["error"]["message"]
    assert result["error"]["how_to_fix"]


def test_post_tweet_lists_every_missing_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch, *TWITTER_FIELDS)
    result = integrations.post_tweet("hello")
    assert result["ok"] is False
    assert result["error"]["code"] == E_INTEGRATION
    assert result["error"]["context"]["missing"] == [
        "TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET",
    ]
    for name in result["error"]["context"]["missing"]:
        assert name in result["error"]["message"]


def test_post_tweet_reports_only_the_fields_actually_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, twitter_api_key="k", twitter_api_secret="s", twitter_access_token="t")
    _clear(monkeypatch, "twitter_access_secret")
    result = integrations.post_tweet("hello")
    assert result["error"]["context"]["missing"] == ["TWITTER_ACCESS_SECRET"]


def test_post_tweet_with_credentials_but_no_library_refuses_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, **{field: "value" for field in TWITTER_FIELDS})
    monkeypatch.setitem(sys.modules, "tweepy", None)  # forces ImportError, as if not installed
    result = integrations.post_tweet("hello")
    assert result["ok"] is False
    assert result["error"]["code"] == E_INTEGRATION
    assert "not installed" in result["error"]["message"]
    assert result["error"]["how_to_fix"] == integrations._TWEEPY_HINT


def test_post_tweet_provider_success_returns_the_provider_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, **{field: f"cred-{field}" for field in TWITTER_FIELDS})
    seen: dict = {}

    class _Response:
        data = {"id": 987654321}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            seen["client_kwargs"] = kwargs

        def create_tweet(self, *, text: str) -> _Response:
            seen["text"] = text
            return _Response()

    fake = types.ModuleType("tweepy")
    fake.Client = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tweepy", fake)

    result = integrations.post_tweet("real tweet 🎉")

    assert result == {"ok": True, "result": {"id": "987654321"}}
    assert seen["text"] == "real tweet 🎉"
    assert seen["client_kwargs"]["consumer_key"] == "cred-twitter_api_key"
    assert seen["client_kwargs"]["access_token_secret"] == "cred-twitter_access_secret"


def test_post_tweet_provider_failure_is_a_named_refusal_with_the_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, **{field: "value" for field in TWITTER_FIELDS})

    class _Client:
        def __init__(self, **kwargs: object) -> None: ...

        def create_tweet(self, *, text: str) -> None:
            raise RuntimeError("429 rate limited by the provider")

    fake = types.ModuleType("tweepy")
    fake.Client = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tweepy", fake)

    result = integrations.post_tweet("hello")
    assert result["ok"] is False
    assert result["error"]["code"] == E_INTEGRATION
    # The provider's own text is kept (never swallowed); the refusal names the call site.
    assert "tweepy.Client failed" in result["error"]["message"]
    assert "429 rate limited by the provider" in result["error"]["message"]


# ── send_email ───────────────────────────────────────────────────────────────────────────────


def test_send_email_refuses_when_only_the_from_address_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, sendgrid_api_key="SG.fake")
    _clear(monkeypatch, "sendgrid_from_email")
    result = integrations.send_email(to="a@example.com", subject="s", body="b")
    assert result["ok"] is False
    assert result["error"]["code"] == E_INTEGRATION
    assert result["error"]["context"]["missing"] == ["SENDGRID_FROM_EMAIL"]


def test_send_email_provider_success_returns_the_wire_status_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, sendgrid_api_key="SG.fake", sendgrid_from_email="from@example.com")
    seen: dict = {}

    class _Client:
        def __init__(self, key: str) -> None:
            seen["key"] = key

        def send(self, message: object) -> object:
            seen["message"] = message
            return types.SimpleNamespace(status_code=202)

    def _mail(**kwargs: object) -> dict:
        seen["mail_kwargs"] = kwargs
        return {"mail": kwargs}

    fake_sendgrid = types.ModuleType("sendgrid")
    fake_sendgrid.SendGridAPIClient = _Client  # type: ignore[attr-defined]
    fake_helpers = types.ModuleType("sendgrid.helpers")
    fake_mail = types.ModuleType("sendgrid.helpers.mail")
    fake_mail.Mail = _mail  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sendgrid", fake_sendgrid)
    monkeypatch.setitem(sys.modules, "sendgrid.helpers", fake_helpers)
    monkeypatch.setitem(sys.modules, "sendgrid.helpers.mail", fake_mail)

    result = integrations.send_email(to="a@example.com", subject="Subj", body="Body")

    assert result["ok"] is True
    assert result["result"]["status_code"] == 202
    # RD14 extended this shape (message_id from the X-Message-Id header) — that is that lane's
    # contract to pin; here the wire status and the keyword plumbing are the assertions.
    assert set(result["result"]) >= {"status_code"}
    assert seen["key"] == "SG.fake"
    assert seen["mail_kwargs"]["to_emails"] == "a@example.com"
    assert seen["mail_kwargs"]["subject"] == "Subj"
    assert seen["mail_kwargs"]["plain_text_content"] == "Body"


# ── github / stripe refusal paths ────────────────────────────────────────────────────────────


def test_open_pull_request_refuses_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "github_token")
    result = integrations.open_pull_request(repo="acme/site", branch="fix", title="t", body="b")
    assert result["ok"] is False
    assert result["error"]["code"] == E_INTEGRATION
    assert "GITHUB_TOKEN" in result["error"]["message"]


def test_refund_payment_refuses_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "stripe_secret_key")
    result = integrations.refund_payment(payment_id="pi_123", amount_cents=500)
    assert result["ok"] is False
    assert result["error"]["code"] == E_INTEGRATION
    assert "STRIPE_SECRET_KEY" in result["error"]["message"]


# ── update_ad_budget (the documented refuse-by-name adapter) ─────────────────────────────────


def test_update_ad_budget_unknown_platform_is_named_validation() -> None:
    result = integrations.update_ad_budget(platform="tiktok", campaign_ref="c-1",
                                           budget_change_usd=10.0)
    assert result["ok"] is False
    assert result["error"]["code"] == E_VALIDATION
    assert "tiktok" in result["error"]["message"]


def test_update_ad_budget_lists_the_missing_google_ads_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch, *GOOGLE_ADS_FIELDS)
    result = integrations.update_ad_budget(platform="google_ads", campaign_ref="c-1",
                                           budget_change_usd=10.0)
    assert result["ok"] is False
    assert result["error"]["code"] == E_INTEGRATION
    assert result["error"]["context"]["missing"] == [
        "GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_ADS_CUSTOMER_ID",
    ]


@pytest.mark.parametrize(("platform", "fields"), [
    ("google_ads", GOOGLE_ADS_FIELDS),
    ("meta_ads", META_ADS_FIELDS),
])
def test_update_ad_budget_fully_configured_still_refuses_by_design(
    monkeypatch: pytest.MonkeyPatch, platform: str, fields: tuple[str, ...]
) -> None:
    """With every credential present the adapter refuses by NAME (no account-scoped client is
    shipped) and records the recommendation — never a fabricated success."""
    _set(monkeypatch, **{field: "value" for field in fields})
    result = integrations.update_ad_budget(platform=platform, campaign_ref="c-1",
                                           budget_change_usd=25.0)
    assert result["ok"] is False
    assert result["error"]["code"] == E_INTEGRATION
    assert "does not ship" in result["error"]["message"]
    assert "recorded" in result["error"]["message"]
    assert result["error"]["context"]["platform"] == platform
    assert result["error"]["context"]["budget_change_usd"] == 25.0
