"""V28/L5 — `spend_usd`: NULL (not measured) and 0 (measured $0.00) stay distinct end to end.

V26/L6 already pins migration 0015's backfill (`test_v26_ads_spend.py`).  The gaps this file
closes, on a database built by the whole chain from zero:

1. at the shipped head, an INSERT that OMITS spend lands as SQL NULL — proof there is no column
   default left anywhere in the chain (0013→0016), not just in 0015's own DDL;
2. a measured `0` lands as `0`, and the GENERATED columns tell the two apart (`cpc` NULL for an
   absent spend, `0.0` for a measured zero; `roas` NULL unless revenue is measured AND spend is a
   usable denominator);
3. the read path (`api/v1/ads.py::_metric_response`) serves `null` and `0.0` distinctly and never
   invents a ROAS from an absent spend.
"""
from __future__ import annotations

import pathlib
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from alembic import command
from alembic import config as alembic_config

BACKEND = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def migrated_db(monkeypatch, tmp_path):
    from app.config import settings

    db_path = tmp_path / "v28_spend.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return db_path


def _seed(con: sqlite3.Connection) -> None:
    con.execute("INSERT INTO ad_campaigns (id, platform, name) VALUES (1, 'google_ads', 'c1')")
    # id 1: spend and revenue OMITTED entirely (not an explicit NULL) — the column default leg.
    con.execute(
        "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions) "
        "VALUES (1, 1, '2026-10-01', 100, 10, 1)"
    )
    # id 2: measured $0.00 spend and revenue.
    con.execute(
        "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions, "
        "spend_usd, revenue_usd) VALUES (2, 1, '2026-10-02', 100, 10, 1, 0, 0)"
    )
    # id 3: revenue measured, spend absent.
    con.execute(
        "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions, "
        "spend_usd, revenue_usd) VALUES (3, 1, '2026-10-03', 100, 10, 1, NULL, 10)"
    )
    # id 4: both measured — the only row that may have a ratio.
    con.execute(
        "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions, "
        "spend_usd, revenue_usd) VALUES (4, 1, '2026-10-04', 100, 10, 1, 2, 10)"
    )
    con.commit()


def test_an_omitted_spend_is_stored_as_null_and_a_measured_zero_as_zero(migrated_db) -> None:
    con = sqlite3.connect(migrated_db.as_posix())
    try:
        _seed(con)
        values = dict(con.execute("SELECT id, spend_usd FROM ad_metrics").fetchall())
        assert values[1] is None, (
            "an INSERT that omits spend must land as NULL — a column default (0015) would make it 0"
        )
        assert values[2] == 0, "a reported 0 is a measurement and must stay 0"
        assert values[3] is None
        assert values[4] == 2

        assert con.execute("SELECT COUNT(*) FROM ad_metrics").fetchone()[0] == 4
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()


def test_generated_ratios_never_invent_a_cost_or_return_from_an_absent_spend(migrated_db) -> None:
    con = sqlite3.connect(migrated_db.as_posix())
    try:
        _seed(con)
        rows = {
            row[0]: {"cpc": row[1], "roas": row[2]}
            for row in con.execute("SELECT id, cpc, roas FROM ad_metrics")
        }

        assert rows[1]["cpc"] is None, "no spend → no cost per click (NOT 0)"
        assert rows[1]["roas"] is None, "no spend/revenue → no ratio"

        assert float(rows[2]["cpc"]) == 0.0, "a measured 0 spend gives a real $0.00 per click"
        assert rows[2]["roas"] is None, "0 spend is not a usable denominator — no ratio"

        assert rows[3]["cpc"] is None, "spend absent → cpc absent, even with revenue present"
        assert rows[3]["roas"] is None

        assert float(rows[4]["cpc"]) == pytest.approx(0.2)
        assert float(rows[4]["roas"]) == pytest.approx(5.0)
    finally:
        con.close()


def test_the_wire_serves_null_and_measured_zero_distinctly() -> None:
    """`_metric_response` is the one place the API turns a stored Numeric into JSON."""
    from app.api.v1.ads import _metric_response
    from app.models.ads import AdMetric

    def metric(spend: Decimal | None, revenue: Decimal | None = None) -> AdMetric:
        return AdMetric(
            id=1,
            campaign_id=1,
            date=date(2026, 10, 1),
            impressions=100,
            clicks=10,
            conversions=1,
            spend_usd=spend,
            revenue_usd=revenue,
        )

    absent = _metric_response(metric(None))
    measured_zero = _metric_response(metric(Decimal("0")))
    measured_pair = _metric_response(metric(Decimal("2"), Decimal("10")))

    assert absent.spend_usd is None, "NULL spend must serialize as null"
    assert measured_zero.spend_usd == 0.0, "a measured zero must serialize as 0.0"
    assert absent.roas is None and measured_zero.roas is None
    assert measured_pair.spend_usd == 2.0
    assert measured_pair.roas == pytest.approx(5.0)
    assert absent.revenue_usd is None and measured_zero.revenue_usd is None
