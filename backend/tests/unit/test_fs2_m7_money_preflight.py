"""PHASE-3 fixer-2 / M7 — the PG money pre-flight names the rows `numeric(14, 4)` cannot hold.

`0002_queue_and_perf.py` converts four money columns with `USING round(x::numeric, 4)` and
`numeric(14, 4)` holds |v| < 10^10; a legacy ±Infinity / NaN / overflow row aborts the ALTER
mid-upgrade.  `_money_preflight(bind)` now runs first and raises with table.column + sample
ids.  The probe SQL is PostgreSQL (cast round semantics), so the runtime leg on PG is UNRUN
here (no PG host) — the exact host command is recorded in the lane receipt; these pins cover
the assertion LOGIC: silent on clean data, loud and specific on offending rows, complete
coverage of the four columns.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2]


def _load_0002():
    path = BACKEND / "alembic" / "versions" / "0002_queue_and_perf.py"
    spec = importlib.util.spec_from_file_location("polsia_migration_0002", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Rows:
    def __init__(self, rows):  # noqa: ANN001
        self._rows = rows

    def fetchall(self):  # noqa: ANN201
        return self._rows


class _FakeBind:
    """Routes the PG probe SQL for each column to canned rows (SQLite never sees this SQL)."""

    def __init__(self, offenders: dict[str, list[tuple[int, object]]]) -> None:
        self._offenders = offenders
        self.queries: list[str] = []

    def exec_driver_sql(self, sql: str):  # noqa: ANN201
        self.queries.append(sql)
        for key, rows in self._offenders.items():
            table, column = key.split(".", 1)
            if f"FROM {table} WHERE {column} IS NOT NULL" in sql:
                return _Rows(rows)
        return _Rows([])


def test_the_preflight_covers_exactly_the_four_money_columns():
    module = _load_0002()
    assert module._MONEY_COLUMNS == (
        ("ad_campaigns", "daily_budget_usd"),
        ("ad_campaigns", "total_spent_usd"),
        ("ad_metrics", "spend_usd"),
        ("agent_runs", "cost_usd"),
    )


def test_the_preflight_is_silent_on_clean_data():
    module = _load_0002()
    bind = _FakeBind({})
    module._money_preflight(bind)
    assert len(bind.queries) == 4, "one probe per money column, no more"


def test_the_preflight_names_columns_and_sample_ids():
    module = _load_0002()
    bind = _FakeBind(
        {
            "ad_campaigns.daily_budget_usd": [(7, 1e10)],
            "agent_runs.cost_usd": [(9, float("nan"))],
        }
    )
    with pytest.raises(RuntimeError) as excinfo:
        module._money_preflight(bind)
    message = str(excinfo.value)
    assert "ad_campaigns.daily_budget_usd" in message and "id=7" in message
    assert "agent_runs.cost_usd" in message and "id=9" in message
    assert "numeric(14, 4)" in message
