"""4-02 + 4-03 (S5-03 + S5-05, branch "a" LOCKED) — the REAL ads/revenue readers — RED-first.

The parent LOCKED both items to branch "a": build the REAL readers (deps ship — google-ads
24.1.0, facebook-business 21.0.0, stripe 11.3.0), with an honest NAMED refusal for the
unconfigured state. Pinned here:

  * `integrations.read_ad_metrics` — a real Google Ads / Meta campaign-metrics read, config-gated
    (missing credentials = E_INTEGRATION naming them, never fabricated metrics); the provider
    libraries are stubbed ONLY at the boundary (the client module), so the pin exercises the real
    query/aggregation code;
  * `integrations.read_revenue_facts` — a real Stripe charges+balance read, config-gated the same
    way (missing STRIPE_SECRET_KEY = named refusal; no snapshot may be born from it);
  * the agents' consume legs: the attached envelope rides the VOLATILE `extra_context` layer as
    quarantined DATA (`wrap_untrusted`), a refusal is NAMED into the prompt (the run never
    fabricates metrics from an unread provider), a malformed attachment is a named E_VALIDATION.
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest

import app.config as app_config
from app.agents import integrations
from app.agents.ads_management import agent as ads_agent_mod
from app.agents.finance import agent as finance_agent_mod
from app.core.errors import E_INTEGRATION, E_VALIDATION, PolsiaError

GOOGLE_ADS_FIELDS = (
    "google_ads_developer_token",
    "google_ads_client_id",
    "google_ads_client_secret",
    "google_ads_refresh_token",
    "google_ads_customer_id",
)
#: The env-facing names the refusal must report (the `missing=[...]` context list).
GOOGLE_ADS_MISSING = (
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
)


def _clear_google_ads(monkeypatch: Any) -> None:
    for field in GOOGLE_ADS_FIELDS:
        monkeypatch.setattr(app_config.settings, field, "")


def _arm_google_ads(monkeypatch: Any) -> None:
    monkeypatch.setattr(app_config.settings, "google_ads_developer_token", "dev-token")
    monkeypatch.setattr(app_config.settings, "google_ads_client_id", "client-id")
    monkeypatch.setattr(app_config.settings, "google_ads_client_secret", "client-secret")
    monkeypatch.setattr(app_config.settings, "google_ads_refresh_token", "refresh-token")
    monkeypatch.setattr(app_config.settings, "google_ads_customer_id", "1234567890")


# ── the boundary doubles: fake provider client modules ONLY (real query code runs) ─────────────


def _install_fake_google_ads(monkeypatch: Any, rows: list[Any]) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _FakeService:
        def search(self, *, customer_id: str, query: str) -> list[Any]:
            captured["customer_id"] = customer_id
            captured["query"] = query
            return rows

    class _FakeClient:
        @classmethod
        def load_from_dict(cls, config: dict[str, Any]) -> _FakeClient:
            captured["config"] = dict(config)
            return cls()

        def get_service(self, name: str) -> _FakeService:
            captured["service"] = name
            return _FakeService()

    client_mod = types.ModuleType("google.ads.googleads.client")
    client_mod.GoogleAdsClient = _FakeClient               # type: ignore[attr-defined]
    for name in ("google", "google.ads", "google.ads.googleads"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "google.ads.googleads.client", client_mod)
    return captured


def _google_row(campaign_id: int, name: str, micros: int, conversions: float,
                value: float) -> Any:
    return types.SimpleNamespace(
        campaign=types.SimpleNamespace(id=campaign_id, name=name, status="ENABLED"),
        metrics=types.SimpleNamespace(cost_micros=micros, conversions=conversions,
                                      conversions_value=value),
    )


# ── 1. ads metrics reader ──────────────────────────────────────────────────────────────────────


def test_ads_reader_refuses_by_name_when_google_ads_is_unconfigured(monkeypatch) -> None:
    _clear_google_ads(monkeypatch)
    out = integrations.read_ad_metrics(platform="google_ads")
    assert out["ok"] is False
    refusal = out["error"]
    assert refusal["code"] == E_INTEGRATION
    assert "not configured" in refusal["message"]
    assert set(refusal["context"]["missing"]) == set(GOOGLE_ADS_MISSING)
    assert refusal["how_to_fix"]


def test_ads_reader_refuses_by_name_when_meta_ads_is_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(app_config.settings, "meta_ads_access_token", "")
    monkeypatch.setattr(app_config.settings, "meta_ads_account_id", "")
    out = integrations.read_ad_metrics(platform="meta_ads")
    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert set(out["error"]["context"]["missing"]) == {"META_ADS_ACCESS_TOKEN",
                                                       "META_ADS_ACCOUNT_ID"}


def test_ads_reader_reads_google_ads_campaign_metrics(monkeypatch) -> None:
    """The REAL reader path: load client -> GAQL search -> per-campaign aggregation."""
    _arm_google_ads(monkeypatch)
    captured = _install_fake_google_ads(monkeypatch, [
        _google_row(1, "Brand", 12_340_000, 3.0, 55.5),
        _google_row(1, "Brand", 1_660_000, 1.0, 10.0),
        _google_row(2, "Prospecting", 5_000_000, 0.0, 0.0),
    ])

    out = integrations.read_ad_metrics(platform="google_ads", days=7)

    assert out["ok"] is True
    result = out["result"]
    assert result["platform"] == "google_ads"
    assert result["days"] == 7
    assert captured["customer_id"] == "1234567890"
    assert captured["service"] == "GoogleAdsService"
    assert "segments.date BETWEEN" in captured["query"]

    by_id = {row["campaign_id"]: row for row in result["campaigns"]}
    assert set(by_id) == {"1", "2"}
    brand = by_id["1"]
    assert brand["spend_cents"] == 14_000_000 // 10_000     # micros -> cents (12.34 + 1.66 USD)
    assert brand["spend_usd"] == 14.0
    assert brand["conversions"] == 4.0
    assert brand["conversions_value_usd"] == 65.5
    assert brand["roas"] == round(65.5 / 14.0, 4)
    assert result["totals"]["spend_cents"] == (14_000_000 + 5_000_000) // 10_000
    assert result["totals"]["roas"] == round(65.5 / 19.0, 4)


def test_ads_reader_reads_meta_insights(monkeypatch) -> None:
    """The Meta leg: FacebookAdsApi.init + AdAccount insights, aggregated the same shape."""
    monkeypatch.setattr(app_config.settings, "meta_ads_access_token", "meta-token")
    monkeypatch.setattr(app_config.settings, "meta_ads_account_id", "987")
    captured: dict[str, Any] = {}

    class _FakeAccount:
        def __init__(self, account_id: str) -> None:
            captured["account_id"] = account_id

        def get_insights(self, *, fields: list[str], params: dict[str, Any]) -> list[Any]:
            captured["fields"] = fields
            captured["params"] = params
            return [{
                "campaign_id": "c-1",
                "campaign_name": "Retargeting",
                "spend": "10.50",
                "actions": [{"action_type": "purchase", "value": "4"}],
                "action_values": [{"action_type": "purchase", "value": "42.00"}],
            }]

    class _FakeApi:
        @classmethod
        def init(cls, **kwargs: Any) -> None:
            captured["token"] = kwargs.get("access_token")

    fb = types.ModuleType("facebook_business")
    fb_api = types.ModuleType("facebook_business.api")
    fb_objs = types.ModuleType("facebook_business.adobjects")
    fb_acc = types.ModuleType("facebook_business.adobjects.adaccount")
    fb_api.FacebookAdsApi = _FakeApi                      # type: ignore[attr-defined]
    fb_acc.AdAccount = _FakeAccount                       # type: ignore[attr-defined]
    fb.api = fb_api                                       # type: ignore[attr-defined]
    fb.adobjects = fb_objs                                # type: ignore[attr-defined]
    for name, module in {
        "facebook_business": fb,
        "facebook_business.api": fb_api,
        "facebook_business.adobjects": fb_objs,
        "facebook_business.adobjects.adaccount": fb_acc,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    out = integrations.read_ad_metrics(platform="meta_ads", days=14)

    assert out["ok"] is True
    result = out["result"]
    assert result["platform"] == "meta_ads"
    assert captured["account_id"] == "act_987"
    assert captured["token"] == "meta-token"
    assert "spend" in captured["fields"]
    assert captured["params"]["level"] == "campaign"
    assert set(captured["params"]["time_range"]) == {"since", "until"}
    campaign = result["campaigns"][0]
    assert campaign["campaign_id"] == "c-1"
    assert campaign["spend_cents"] == 1050
    assert campaign["spend_usd"] == 10.5
    assert campaign["conversions"] == 4.0
    assert campaign["conversions_value_usd"] == 42.0
    assert campaign["roas"] == 4.0


def test_ads_reader_refuses_an_unknown_platform_by_name() -> None:
    out = integrations.read_ad_metrics(platform="tiktok_ads")
    assert out["ok"] is False
    assert out["error"]["code"] == E_VALIDATION
    assert "google_ads" in out["error"]["how_to_fix"]


# ── 2. the ads consume leg (quarantined extra_context) ─────────────────────────────────────────


def _ads_attach(result: dict[str, Any] | None = None, refusal: dict[str, Any] | None = None) -> Any:
    envelope = {"ok": True, "result": result} if result is not None else {"ok": False,
                                                                          "error": refusal}
    return {"title": "Sync ad metrics", "metadata": {ads_agent_mod.AD_METRICS_KEY: envelope}}


def test_ads_metrics_extra_context_quarantines_measured_facts() -> None:
    task = _ads_attach(result={
        "platform": "google_ads", "days": 7, "fetched_at": "2026-10-15T00:00:00+00:00",
        "campaigns": [{"campaign_id": "1", "name": "Brand", "spend_cents": 1234,
                       "spend_usd": 12.34, "roas": 4.4975}],
        "totals": {"spend_cents": 1234, "spend_usd": 12.34, "roas": 4.4975},
    })
    text = ads_agent_mod.ad_metrics_extra_context(task)
    assert text is not None
    assert '<untrusted source="google_ads"' in text
    assert "12.34" in text and "4.4975" in text


def test_ads_metrics_extra_context_names_a_refusal_instead_of_inventing_numbers() -> None:
    task = _ads_attach(refusal={"code": E_INTEGRATION,
                                "message": "google_ads metrics read is not configured: "
                                           "GOOGLE_ADS_DEVELOPER_TOKEN",
                                "how_to_fix": "set the credentials",
                                "context": {"missing": ["GOOGLE_ADS_DEVELOPER_TOKEN"]}})
    text = ads_agent_mod.ad_metrics_extra_context(task)
    assert text is not None
    assert "REFUSED" in text and E_INTEGRATION in text
    assert "not configured" in text


def test_the_metrics_attach_is_refused_when_malformed() -> None:
    for bad in ("nope", {"ok": "yes"}, {"ok": True, "result": "not-a-dict"}):
        task = {"title": "Sync ad metrics", "metadata": {ads_agent_mod.AD_METRICS_KEY: bad}}
        with pytest.raises(PolsiaError) as refusal:
            ads_agent_mod.ad_metrics_extra_context(task)
        assert refusal.value.code == E_VALIDATION


def test_ads_metrics_extra_context_absent_is_none() -> None:
    assert ads_agent_mod.ad_metrics_extra_context({"title": "Sync ad metrics"}) is None
    assert ads_agent_mod.ad_metrics_extra_context({}) is None


def test_ads_run_injects_the_attached_metrics_into_the_prompt(monkeypatch) -> None:
    """The run consumes the attach: `collect_structured` receives it as `extra_context` (the
    volatile, quarantined layer) — the input leg the ROLE now truthfully claims."""
    captured: dict[str, Any] = {}
    agent = ads_agent_mod.AdsManagementAgent()

    def fake_collect(task: dict[str, Any], context: dict[str, Any], **kw: Any) -> Any:
        captured.update(kw)
        return ({"summary": "nothing to change"}, None)

    agent.collect_structured = fake_collect
    task = _ads_attach(result={"platform": "google_ads", "days": 7,
                               "campaigns": [{"campaign_id": "1", "name": "Brand",
                                              "spend_usd": 12.34}],
                               "totals": {"spend_usd": 12.34}})

    out = agent.run(task, {})

    assert "extra_context" in captured, "the attach never reached the prompt"
    assert "12.34" in captured["extra_context"]
    assert out["summary"] == "nothing to change"


# ── 3. the revenue-facts reader ────────────────────────────────────────────────────────────────


def _install_fake_stripe(monkeypatch: Any, charges: list[dict[str, Any]],
                         balance: dict[str, Any]) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _ChargeList:
        def auto_paging_iter(self) -> Any:
            return iter(charges)

    class _Charge:
        @staticmethod
        def list(**kwargs: Any) -> _ChargeList:
            captured["charge_list_kwargs"] = kwargs
            return _ChargeList()

    class _Balance:
        @staticmethod
        def retrieve() -> dict[str, Any]:
            return balance

    stripe_mod = types.ModuleType("stripe")
    stripe_mod.api_key = ""
    stripe_mod.Charge = _Charge                            # type: ignore[attr-defined]
    stripe_mod.Balance = _Balance                          # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "stripe", stripe_mod)
    captured["module"] = stripe_mod
    return captured


def test_revenue_reader_refuses_by_name_when_stripe_is_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(app_config.settings, "stripe_secret_key", "")
    out = integrations.read_revenue_facts()
    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert out["error"]["context"]["missing"] == ["STRIPE_SECRET_KEY"]


def test_revenue_reader_reads_stripe_charges_and_balance(monkeypatch) -> None:
    monkeypatch.setattr(app_config.settings, "stripe_secret_key", "sk_test_fake")
    captured = _install_fake_stripe(
        monkeypatch,
        charges=[
            {"id": "ch_1", "amount": 1500, "amount_refunded": 0, "currency": "usd",
             "status": "succeeded"},
            {"id": "ch_2", "amount": 5000, "amount_refunded": 500, "currency": "usd",
             "status": "succeeded"},
            {"id": "ch_3", "amount": 999, "amount_refunded": 0, "currency": "usd",
             "status": "failed"},
        ],
        balance={"available": [{"amount": 4200, "currency": "usd"}], "pending": []},
    )

    out = integrations.read_revenue_facts(days=30)

    assert out["ok"] is True
    assert captured["module"].api_key == "sk_test_fake"
    assert "created" in captured["charge_list_kwargs"]                 # a real window bound
    result = out["result"]
    usd = result["charges_by_currency"]["usd"]
    assert usd["count"] == 2                                           # failed charge not counted
    assert usd["gross_cents"] == 6500
    assert usd["refunded_cents"] == 500
    assert usd["net_cents"] == 6000
    assert result["balance_available"] == [{"amount_cents": 4200, "currency": "usd"}]
    assert result["currencies"] == ["usd"]


def test_revenue_facts_extra_context_quarantines_measured_facts() -> None:
    task = {"title": "Daily revenue snapshot", "metadata": {finance_agent_mod.REVENUE_FACTS_KEY:
            {"ok": True, "result": {"fetched_at": "2026-10-15T00:00:00+00:00", "days": 30,
                                    "charges_by_currency": {"usd": {"count": 2,
                                                                    "gross_cents": 6500,
                                                                    "refunded_cents": 500,
                                                                    "net_cents": 6000}},
                                    "balance_available": [{"amount_cents": 4200,
                                                           "currency": "usd"}],
                                    "currencies": ["usd"]}}}}
    text = finance_agent_mod.revenue_facts_extra_context(task)
    assert text is not None
    assert "<untrusted source=" in text
    assert "6500" in text and "6000" in text and "4200" in text


def test_revenue_facts_extra_context_names_a_refusal_instead_of_inventing_numbers() -> None:
    task = {"title": "Daily revenue snapshot",
            "metadata": {finance_agent_mod.REVENUE_FACTS_KEY:
                         {"ok": False, "error": {"code": E_INTEGRATION,
                                                 "message": "Stripe revenue read is not "
                                                            "configured: STRIPE_SECRET_KEY",
                                                 "how_to_fix": "set STRIPE_SECRET_KEY"}}}}
    text = finance_agent_mod.revenue_facts_extra_context(task)
    assert "REFUSED" in text and E_INTEGRATION in text


def test_revenue_facts_attach_is_refused_when_malformed() -> None:
    task = {"title": "x", "metadata": {finance_agent_mod.REVENUE_FACTS_KEY: ["nope"]}}
    with pytest.raises(PolsiaError) as refusal:
        finance_agent_mod.revenue_facts_extra_context(task)
    assert refusal.value.code == E_VALIDATION


def test_finance_run_injects_the_attached_revenue_facts_into_the_prompt(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    agent = finance_agent_mod.FinanceAgent()

    def fake_collect(task: dict[str, Any], context: dict[str, Any], **kw: Any) -> Any:
        captured.update(kw)
        return ({"summary": "reconciled", "mrr_cents": 4200}, None)

    agent.collect_structured = fake_collect
    task = {"title": "Daily revenue snapshot",
            "metadata": {finance_agent_mod.REVENUE_FACTS_KEY:
                         {"ok": True, "result": {"charges_by_currency": {"usd": {
                             "net_cents": 6000}}, "currencies": ["usd"]}}}}

    out = agent.run(task, {})

    assert "extra_context" in captured, "the attach never reached the prompt"
    assert "6000" in captured["extra_context"]
    assert out["snapshot"]["mrr_cents"] == 4200
