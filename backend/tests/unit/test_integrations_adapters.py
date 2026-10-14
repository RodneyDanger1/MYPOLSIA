"""RD15 dead-sweep — `app/agents/integrations.py`: the LIVE refusal branches + `integration_call`.

Why this file exists: the audit coverage snapshot (`F-coverage/raw/coverage-before.json`) shows
38.6% (17/44) for this module with the missing lines being exactly the branches THIS deployment
takes — the credential refusals, the platform guards and the ads branches. Those are the honesty
contract in action: a capability that is not configured must refuse BY NAME, never return a
success-shaped empty result. They are pinned here.

No test in this file can touch the network: the provider-call helpers (`_tweepy_create_tweet`,
`_sendgrid_send`, `_pygithub_open_pr`, `_stripe_refund`) are `# pragma: no cover` on purpose
(real credentials required) and are never invoked; every "configured" leg routes through a
recorder placed in front of `integration_call`.

Production call sites of this module (all six behind `if outcome.allowed:` — the firewall — read
2026-10-13): social_media/agent.py:108, email_outreach/agent.py:84, customer_support/agent.py:127,
code_generation/agent.py:99, finance/agent.py:163, ads_management/agent.py:100.
"""
from __future__ import annotations

from typing import Any

import app.config as app_config
from app.agents import integrations
from app.agents.side_effects import integration_call
from app.core.errors import E_INTEGRATION, E_VALIDATION

TWITTER_FIELDS = (
    "twitter_api_key", "twitter_api_secret", "twitter_access_token", "twitter_access_secret",
)
GOOGLE_ADS_FIELDS = (
    "google_ads_developer_token", "google_ads_client_id", "google_ads_refresh_token",
    "google_ads_customer_id",
)
META_ADS_FIELDS = ("meta_ads_access_token", "meta_ads_account_id")


def _set(monkeypatch, fields: tuple[str, ...], value: str) -> None:
    for field in fields:
        monkeypatch.setattr(app_config.settings, field, value)


class _Recorder:
    """Stands in for `integration_call`: records the routed arguments, returns a sentinel."""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = {"ok": True, "result": {"id": "provider-ref-1"}} if result is None else result

    def __call__(self, module_name: str, attribute: str, **kwargs: Any) -> dict:
        self.calls.append({"module_name": module_name, "attribute": attribute, **kwargs})
        return self.result


# --------------------------------------------------------------- post_tweet

def test_post_tweet_refuses_any_platform_other_than_twitter():
    """The platform guard is a named E_VALIDATION refusal, not a silent coercion.

    U5/P1 pin move (old -> new): this used `platform="linkedin"`, which U5 wired to a real
    adapter — the pinned SUBJECT (an unwired platform refuses by name) is unchanged, the platform
    value moved to one that stays unwired (`myspace`). The linkedin ROUTE is pinned in
    `tests/unit/test_u5_social_channels.py`."""
    out = integrations.post_tweet("hello", platform="myspace")
    assert out["ok"] is False
    err = out["error"]
    assert err["code"] == E_VALIDATION
    assert "does not handle platform 'myspace'" in err["message"]
    assert err["context"] == {"platform": "myspace"}
    assert err["how_to_fix"]


def test_post_tweet_missing_credentials_names_every_variable(monkeypatch):
    _set(monkeypatch, TWITTER_FIELDS, "")
    out = integrations.post_tweet("hello")
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert err["context"]["missing"] == [
        "TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET",
    ]
    for name in err["context"]["missing"]:
        assert name in err["message"]
    assert "pip install tweepy" in err["how_to_fix"]


def test_post_tweet_with_credentials_routes_to_the_tweepy_adapter(monkeypatch):
    _set(monkeypatch, TWITTER_FIELDS, "test-value")
    recorder = _Recorder()
    monkeypatch.setattr(integrations, "integration_call", recorder)
    out = integrations.post_tweet("hello riders", platform="twitter")
    assert out == {"ok": True, "result": {"id": "provider-ref-1"}}
    (call,) = recorder.calls
    assert call["module_name"] == "tweepy" and call["attribute"] == "Client"
    assert call["agent_type"] == "social_media"
    assert call["text"] == "hello riders"
    assert call["call"] is integrations._tweepy_create_tweet
    assert call["install_hint"]  # the refusal hint travels with the call


# --------------------------------------------------------------- post_tweet: tiktok / youtube
# V8/T4: the two adapters route through the SAME dispatch table; the "configured" legs stand in a
# recorder in front of `integration_call` (this file's idiom) so the routed adapter helper and its
# arguments are pinned without touching the provider.

def test_post_tiktok_requires_a_media_url_by_name(monkeypatch):
    monkeypatch.setattr(app_config.settings, "tiktok_access_token", "test-value")
    out = integrations.post_tweet("hello", platform="tiktok")
    err = out["error"]
    assert err["code"] == E_VALIDATION
    assert "tiktok" in err["message"] and "image_url" in err["message"]
    assert err["how_to_fix"]


def test_post_tiktok_missing_token_names_it(monkeypatch):
    monkeypatch.setattr(app_config.settings, "tiktok_access_token", "")
    out = integrations.post_tweet("hello", platform="tiktok",
                                  image_url="https://cdn.example.com/v.mp4")
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert err["context"]["missing"] == ["TIKTOK_ACCESS_TOKEN"]
    assert "TIKTOK_ACCESS_TOKEN" in err["how_to_fix"]


def test_post_tiktok_with_token_routes_to_the_content_posting_adapter(monkeypatch):
    monkeypatch.setattr(app_config.settings, "tiktok_access_token", "test-value")
    recorder = _Recorder()
    monkeypatch.setattr(integrations, "integration_call", recorder)
    out = integrations.post_tweet("hello tt", platform="tiktok",
                                  image_url="https://cdn.example.com/v.mp4")
    assert out == {"ok": True, "result": {"id": "provider-ref-1"}}
    (call,) = recorder.calls
    assert call["module_name"] == "httpx" and call["attribute"] == "post"
    assert call["agent_type"] == "social_media"
    assert call["call"] is integrations._tiktok_publish_video
    assert call["text"] == "hello tt"
    assert call["image_url"] == "https://cdn.example.com/v.mp4"


def test_post_youtube_requires_a_media_url_by_name(monkeypatch):
    monkeypatch.setattr(app_config.settings, "youtube_access_token", "test-value")
    out = integrations.post_tweet("hello", platform="youtube")
    err = out["error"]
    assert err["code"] == E_VALIDATION
    assert "youtube" in err["message"] and "image_url" in err["message"]
    assert err["how_to_fix"]


def test_post_youtube_missing_token_names_it(monkeypatch):
    monkeypatch.setattr(app_config.settings, "youtube_access_token", "")
    out = integrations.post_tweet("hello", platform="youtube",
                                  image_url="https://cdn.example.com/v.mp4")
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert err["context"]["missing"] == ["YOUTUBE_ACCESS_TOKEN"]
    assert "YOUTUBE_ACCESS_TOKEN" in err["how_to_fix"]


def test_post_youtube_with_token_routes_to_the_videos_insert_adapter(monkeypatch):
    monkeypatch.setattr(app_config.settings, "youtube_access_token", "test-value")
    recorder = _Recorder()
    monkeypatch.setattr(integrations, "integration_call", recorder)
    out = integrations.post_tweet("hello yt", platform="youtube",
                                  image_url="https://cdn.example.com/v.mp4")
    assert out == {"ok": True, "result": {"id": "provider-ref-1"}}
    (call,) = recorder.calls
    assert call["module_name"] == "httpx" and call["attribute"] == "post"
    assert call["agent_type"] == "social_media"
    assert call["call"] is integrations._youtube_insert_video
    assert call["text"] == "hello yt"
    assert call["image_url"] == "https://cdn.example.com/v.mp4"


# --------------------------------------------------------------- send_email

def test_send_email_missing_credentials_names_them(monkeypatch):
    _set(monkeypatch, ("sendgrid_api_key", "sendgrid_from_email"), "")
    out = integrations.send_email(to="a@example.com", subject="s", body="b")
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert err["context"]["missing"] == ["SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL"]
    assert "pip install sendgrid" in err["how_to_fix"]


def test_send_email_with_credentials_routes_to_sendgrid(monkeypatch):
    _set(monkeypatch, ("sendgrid_api_key", "sendgrid_from_email"), "test-value")
    recorder = _Recorder()
    monkeypatch.setattr(integrations, "integration_call", recorder)
    out = integrations.send_email(to="a@example.com", subject="subj", body="body")
    assert out["ok"] is True
    (call,) = recorder.calls
    assert call["module_name"] == "sendgrid" and call["attribute"] == "SendGridAPIClient"
    assert (call["to"], call["subject"], call["body"]) == ("a@example.com", "subj", "body")


# --------------------------------------------------------------- open_pull_request

def test_open_pull_request_missing_token(monkeypatch):
    monkeypatch.setattr(app_config.settings, "github_token", "")
    out = integrations.open_pull_request(repo="acme/site", branch="feat/x", title="t", body="b")
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert err["context"]["missing"] == ["GITHUB_TOKEN"]
    assert "pip install PyGithub" in err["how_to_fix"]


def test_open_pull_request_with_token_routes_to_pygithub(monkeypatch):
    monkeypatch.setattr(app_config.settings, "github_token", "test-value")
    recorder = _Recorder()
    monkeypatch.setattr(integrations, "integration_call", recorder)
    out = integrations.open_pull_request(repo="acme/site", branch="feat/x", title="t", body="b")
    assert out["ok"] is True
    (call,) = recorder.calls
    assert call["module_name"] == "github" and call["attribute"] == "Github"
    assert call["repo"] == "acme/site" and call["branch"] == "feat/x"
    assert call["base"] == "main"  # documented default travels through untouched


# --------------------------------------------------------------- refund_payment

def test_refund_payment_missing_key(monkeypatch):
    monkeypatch.setattr(app_config.settings, "stripe_secret_key", "")
    out = integrations.refund_payment(payment_id="pi_1")
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert err["context"]["missing"] == ["STRIPE_SECRET_KEY"]
    assert "pip install stripe" in err["how_to_fix"]


def test_refund_payment_with_key_routes_to_stripe_and_keeps_amount_semantics(monkeypatch):
    monkeypatch.setattr(app_config.settings, "stripe_secret_key", "test-value")
    recorder = _Recorder()
    monkeypatch.setattr(integrations, "integration_call", recorder)
    integrations.refund_payment(payment_id="pi_1", amount_cents=250)
    integrations.refund_payment(payment_id="pi_2")  # amount omitted
    first, second = recorder.calls
    assert first["module_name"] == "stripe" and first["attribute"] == "Refund"
    assert first["payment_id"] == "pi_1" and first["amount_cents"] == 250
    assert second["payment_id"] == "pi_2" and second["amount_cents"] is None
    assert all(c["agent_type"] == "finance" for c in recorder.calls)


# --------------------------------------------------------------- update_ad_budget

def test_update_ad_budget_unknown_platform_is_a_named_refusal():
    out = integrations.update_ad_budget(platform="tiktok", campaign_ref="c-1", budget_change_usd=1.0)
    err = out["error"]
    assert err["code"] == E_VALIDATION
    assert "unknown ad platform 'tiktok'" in err["message"]
    assert "google_ads or meta_ads" in err["how_to_fix"]


def test_update_ad_budget_google_ads_missing_config_names_each_var(monkeypatch):
    _set(monkeypatch, GOOGLE_ADS_FIELDS, "")
    out = integrations.update_ad_budget(platform="google_ads", campaign_ref="c-1", budget_change_usd=2.5)
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert err["context"]["missing"] == [
        "GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_ADS_CUSTOMER_ID",
    ]
    assert err["context"]["campaign_ref"] == "c-1"


def test_update_ad_budget_meta_ads_missing_config(monkeypatch):
    _set(monkeypatch, META_ADS_FIELDS, "")
    out = integrations.update_ad_budget(platform="meta_ads", campaign_ref="c-2", budget_change_usd=1.0)
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert err["context"]["missing"] == ["META_ADS_ACCESS_TOKEN", "META_ADS_ACCOUNT_ID"]


def test_update_ad_budget_stays_refused_even_when_fully_configured(monkeypatch):
    """The documented never-pretends branch: with every credential set the adapter STILL refuses
    by name (no account-scoped client ships), carrying the recommendation context back."""
    _set(monkeypatch, GOOGLE_ADS_FIELDS, "test-value")
    out = integrations.update_ad_budget(
        platform="google_ads", campaign_ref="c-9", budget_change_usd=3.0
    )
    assert out["ok"] is False
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert "does not ship" in err["message"] and "recorded instead" in err["message"]
    assert err["context"]["campaign_ref"] == "c-9"
    assert err["context"]["budget_change_usd"] == 3.0


# --------------------------------------------------------------- integration_call itself

def test_integration_call_performs_a_real_lazy_import_and_call():
    # `json.dumps(obj=...)` is a REAL stdlib call with a keyword parameter; the envelope must
    # wrap its actual return value (not a marker), proving the success path is a performed call.
    out = integration_call("json", "dumps", agent_type="rd15", install_hint="", obj={"a": 1})
    assert out == {"ok": True, "result": '{"a": 1}'}


def test_integration_call_missing_library_is_a_named_refusal():
    out = integration_call(
        "zzz_absent_module_rd15", "X", agent_type="rd15", install_hint="pip install zzz"
    )
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert "not installed" in err["message"] and "'zzz_absent_module_rd15'" in err["message"]
    assert err["context"]["module"] == "zzz_absent_module_rd15"
    assert err["how_to_fix"] == "pip install zzz"


def test_integration_call_missing_attribute_is_a_named_refusal():
    out = integration_call("os", "definitely_not_here_rd15", agent_type="rd15", install_hint="hint")
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert "os.definitely_not_here_rd15 does not exist" in err["message"]


def test_integration_call_provider_failure_is_named_never_swallowed():
    def boom(**_kwargs):
        raise RuntimeError("provider exploded")

    out = integration_call("os", "unused", agent_type="rd15", install_hint="hint", call=boom)
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert "provider exploded" in err["message"]
    assert err["context"]["attribute"] == "unused"
