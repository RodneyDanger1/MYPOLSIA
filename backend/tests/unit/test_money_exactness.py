"""T17 / A-T17 — money is exact at rest, and every write crosses ONE boundary.

Measured basis (archived in `polsia-work/build/T17-money/raw/probe_numeric_sqlite.txt`):
`str(Numeric(14, 4))` renders `NUMERIC(14, 4)`; a SQLite round-trip stores `Decimal("1.005")`
and reads back `Decimal("1.0050")`; `SUM` over the column is a `Decimal`.  The DDL itself is
pinned by `test_models_schema_frozen.py`; this file pins the value contract and the single
conversion site.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Numeric, create_engine, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.ads import AdCampaign, AdMetric
from app.models.task import AgentRun
from app.services.task_service import _as_column_decimal

BACKEND = Path(__file__).resolve().parents[2]

_MONEY_COLUMNS = (
    (AgentRun, "cost_usd"),
    (AdCampaign, "daily_budget_usd"),
    (AdCampaign, "total_spent_usd"),
    (AdMetric, "spend_usd"),
)


def test_the_four_money_columns_are_numeric_14_4() -> None:
    for cls, name in _MONEY_COLUMNS:
        column = cls.__table__.c[name]
        assert isinstance(column.type, Numeric), (
            f"{cls.__tablename__}.{name} must be Numeric, is {column.type!r}"
        )
        assert (column.type.precision, column.type.scale) == (14, 4)


def test_the_boundary_converts_once_to_decimal() -> None:
    assert _as_column_decimal(None) is None
    exact = Decimal("1.005")
    assert _as_column_decimal(exact) is exact       # a Decimal is not re-coerced
    got = _as_column_decimal(1.25)                  # a float goes through str(), never arithmetic
    assert isinstance(got, Decimal)
    assert got == Decimal("1.25")


def test_fractional_cent_round_trips_through_the_column(tmp_path: Path) -> None:
    """The defect class this task exists for: 1.005 through a Float column is 1.0049999999…"""
    engine = create_engine(f"sqlite:///{tmp_path / 'money.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(AgentRun(agent_type="finance", cost_usd=Decimal("1.005")))
        session.commit()
        got = session.execute(select(AgentRun.cost_usd)).scalar_one()
        # measured read-back (probe): scale-4 quantization -> Decimal('1.0050'), never a float
        assert isinstance(got, Decimal)
        assert got == Decimal("1.0050")


def test_money_never_crosses_to_a_column_as_a_float() -> None:
    """A-T17 single-site rule: exactly one boundary, and no site writes around it."""
    task_service = (BACKEND / "app" / "services" / "task_service.py").read_text(encoding="utf-8")
    agent_tasks = (BACKEND / "celery_app" / "tasks" / "agent_tasks.py").read_text(encoding="utf-8")
    combined = task_service + agent_tasks

    assert "_as_column_float" not in combined, "the old lossy boundary name must be gone"
    assert "cost_usd=_as_column_decimal(cost_usd)" in task_service
    assert "run.cost_usd = _as_column_decimal(cost_usd)" in task_service
    assert "run_row.cost_usd = _as_column_decimal(cost)" in agent_tasks
    bypass = re.search(r"\.cost_usd\s*=(?!\s*_as_column_decimal)", combined)
    assert bypass is None, f"a money write bypasses the single boundary: {bypass.group(0)!r}"
