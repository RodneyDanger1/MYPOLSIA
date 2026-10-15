"""WAVE V15 / E3 items 4-02 + 4-03 — the ads/revenue ATTACH legs at the 6-hourly dispatch.

RED-first pins for the repair unit (r-repair) completing E3's ready-to-execute attach halves
(`wave-v15/e3-runtime-serial/RECEIPT.md` §Handoff states; interfaces = E2 receipt §3 H2/H3):

  * 4-02 — the ads task carries `metadata.ad_metrics` = the frozen reader's envelope VERBATIM.
    PARENT DECISION: both-platform capable — Google Ads and Meta are both supported and the
    dispatch attaches whichever platform is CONFIGURED (google_ads first — the reader's own
    default — then meta_ads).  A read of an unconfigured platform refuses BY NAME before any
    provider call, so when NEITHER is configured the attach is ONE named refusal envelope naming
    both platforms, with zero provider calls made.
  * 4-03 — the finance task merges `metadata.revenue_facts` = `read_revenue_facts(days=30)`
    into the SAME metadata dict as `failed_payments` (merge — never replace; integer cents).

Harness: the T14b file's (`queue_db` + the `celery.group` stub).  Provider libraries are
stubbed ONLY at the client boundary (`integrations.integration_call`); the readers run for real,
so "configured" detection is exercised through the E2 readers' own credential gates.
"""
from __future__ import annotations

import json
from typing import Any

from app.config import settings

# ── credential fixtures (exactly the fields the E2 readers' gates read) ───────────────────────

_GOOGLE_KEYS = (
    "google_ads_developer_token",
    "google_ads_client_id",
    "google_ads_client_secret",
    "google_ads_refresh_token",
    "google_ads_customer_id",
)
_META_KEYS = ("meta_ads_access_token", "meta_ads_account_id")


def _clear_all_provider_credentials(monkeypatch) -> None:
    """No ad platform and no Stripe configured — every read short-circuits by name."""
    for name in (*_GOOGLE_KEYS, *_META_KEYS, "stripe_secret_key"):
        monkeypatch.setattr(settings, name, "")


def _configure_google(monkeypatch) -> None:
    for name in _GOOGLE_KEYS:
        monkeypatch.setattr(settings, name, f"test-{name}")


def _configure_meta(monkeypatch) -> None:
    monkeypatch.setattr(settings, "meta_ads_access_token", "test-meta-token")
    monkeypatch.setattr(settings, "meta_ads_account_id", "act_test_123")


# ── the provider-client boundary stub (lazy-import contract mirrored) ─────────────────────────


class _IntegrationRecorder:
    """Stands in for `integrations.integration_call` — the ONLY provider boundary the readers
    cross.  `explode=True` is the zero-provider-call probe: any attempt raises loudly."""

    def __init__(self, result: dict[str, Any] | None = None, *, explode: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result if result is not None else {"stub": True}
        self.explode = explode

    def __call__(self, module_name: str, attribute: str, *, agent_type: str,
                 install_hint: str, call: Any = None, **kwargs: Any) -> dict[str, Any]:
        if self.explode:  # pragma: no cover - the failure mode under test
            raise AssertionError(f"provider call attempted: {module_name}.{attribute}")
        self.calls.append({"module": module_name, "attribute": attribute, "kwargs": kwargs})
        return {"ok": True, "result": self.result}


# ── harness helpers (the T14b shapes) ─────────────────────────────────────────────────────────


class _Group:
    """The celery group seam (the sync creates TWO tasks -> `enqueue_created`'s group path)."""

    def __call__(self, sigs):  # noqa: ANN001
        return self

    def apply_async(self) -> None:
        return None


def _metadata_by_agent(queue_db) -> dict[str, Any]:
    rows = queue_db.query("SELECT agent_type, metadata FROM tasks")
    return {row["agent_type"]: row["metadata"] for row in rows}


def _parsed(metadata: Any) -> dict[str, Any]:
    return json.loads(metadata) if metadata and metadata != "null" else {}


# ── 4-02: both-platform attach ────────────────────────────────────────────────────────────────


def test_the_ads_sync_attaches_google_metrics_when_google_is_configured(queue_db, monkeypatch):
    from app.agents import integrations
    from celery_app.tasks.agent_tasks import _ads_stripe_sync

    _clear_all_provider_credentials(monkeypatch)
    _configure_google(monkeypatch)
    recorder = _IntegrationRecorder({"platform": "google_ads", "days": 7,
                                     "campaigns": [], "totals": {}})
    monkeypatch.setattr("celery.group", _Group())
    monkeypatch.setattr(integrations, "integration_call", recorder)

    result = queue_db.run(_ads_stripe_sync())

    assert result["status"] == "queued", result
    attached = _parsed(_metadata_by_agent(queue_db)["ads_management"]).get("ad_metrics")
    assert attached is not None, "the ads dispatch attached NO ad_metrics envelope"
    assert attached == {"ok": True, "result": {"platform": "google_ads", "days": 7,
                                               "campaigns": [], "totals": {}}}
    assert [c["module"] for c in recorder.calls] == ["google.ads.googleads.client"], (
        "google configured -> exactly the google read; meta must not be probed around it"
    )
    assert recorder.calls[0]["kwargs"] == {"days": 7}


def test_the_ads_sync_attaches_meta_metrics_when_only_meta_is_configured(queue_db, monkeypatch):
    from app.agents import integrations
    from celery_app.tasks.agent_tasks import _ads_stripe_sync

    _clear_all_provider_credentials(monkeypatch)
    _configure_meta(monkeypatch)
    recorder = _IntegrationRecorder({"platform": "meta_ads", "days": 7,
                                     "campaigns": [], "totals": {}})
    monkeypatch.setattr("celery.group", _Group())
    monkeypatch.setattr(integrations, "integration_call", recorder)

    result = queue_db.run(_ads_stripe_sync())

    assert result["status"] == "queued", result
    attached = _parsed(_metadata_by_agent(queue_db)["ads_management"]).get("ad_metrics")
    assert attached is not None, "the ads dispatch attached NO ad_metrics envelope"
    assert attached["ok"] is True
    assert attached["result"]["platform"] == "meta_ads", (
        "meta is the configured platform — its read is the one attached"
    )
    assert [c["module"] for c in recorder.calls] == ["facebook_business.adobjects.adaccount"]


def test_the_ads_sync_refuses_by_name_with_zero_provider_calls_when_none_configured(
    queue_db, monkeypatch
):
    from app.agents import integrations
    from celery_app.tasks.agent_tasks import _ads_stripe_sync

    _clear_all_provider_credentials(monkeypatch)
    recorder = _IntegrationRecorder(explode=True)  # any provider call fails the test
    monkeypatch.setattr("celery.group", _Group())
    monkeypatch.setattr(integrations, "integration_call", recorder)

    result = queue_db.run(_ads_stripe_sync())

    assert result["status"] == "queued", result
    attached = _parsed(_metadata_by_agent(queue_db)["ads_management"]).get("ad_metrics")
    assert attached is not None, "no platform configured must still be a NAMED refusal attach"
    assert attached["ok"] is False
    error = attached["error"]
    assert error["code"] == "E_INTEGRATION"
    assert "google_ads" in error["message"] and "meta_ads" in error["message"], (
        "the refusal names BOTH platforms this dispatch supports"
    )
    assert "GOOGLE_ADS_DEVELOPER_TOKEN" in error["message"]
    assert "META_ADS_ACCESS_TOKEN" in error["message"]
    assert error["how_to_fix"]
    assert recorder.calls == [], "an unconfigured read must refuse before any provider call"


# ── 4-03: revenue-facts attach (merge, never replace) ─────────────────────────────────────────


def test_the_ads_sync_attaches_revenue_facts_verbatim(queue_db, monkeypatch):
    from app.agents import integrations
    from celery_app.tasks.agent_tasks import _ads_stripe_sync

    _clear_all_provider_credentials(monkeypatch)
    envelope = {
        "ok": True,
        "result": {
            "days": 30,
            "charges_by_currency": {"usd": {"count": 2, "gross_cents": 4200,
                                            "refunded_cents": 500, "net_cents": 3700}},
            "currencies": ["usd"],
            "balance_available": [{"amount_cents": 999, "currency": "usd"}],
        },
    }
    calls: list[dict[str, Any]] = []

    def fake_revenue(*, days: int = 30) -> dict[str, Any]:
        calls.append({"days": days})
        return envelope

    monkeypatch.setattr("celery.group", _Group())
    monkeypatch.setattr(integrations, "read_revenue_facts", fake_revenue)

    result = queue_db.run(_ads_stripe_sync())

    assert result["status"] == "queued", result
    finance = _parsed(_metadata_by_agent(queue_db)["finance"])
    assert finance.get("revenue_facts") == envelope, (
        "the reader's envelope is attached VERBATIM (integer cents, nothing reshaped)"
    )
    assert "failed_payments" not in finance, (
        "an empty failed-payments read still attaches nothing for that key"
    )
    assert calls == [{"days": 30}], "the H3 contract: read_revenue_facts(days=30)"
