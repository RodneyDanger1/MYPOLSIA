"""R-W5-CA-1 (wave 4's R-W4-CA-1) — the `competitors` table is write-only by construction.

The decision this file PINS: wave 5 does **not** land a writer for a table nothing reads.  The
wave-4 capability audit found the table has no producer in the worker and no consumer in the
product; the follow-up claim then said a "generic `/data/{table}` dump" reads it — which was also
false.  The live OpenAPI carries exactly two `/data` routes (`/api/v1/data`, `/api/v1/data/export`),
zero `{table}` paths and zero competitor routes (`forge/raw/w5/live-data-routes.txt`, 92 paths).

So the honest state is: the table is REFERENCED only by the data-wipe member set (which deletes it),
NOT by the export, and it has no writer.  These pins make a future reader or writer impossible to add
silently: if one lands, this file fails and the claims + the REQUESTS entry must be revisited
together — which is exactly the discipline that keeps a capability claim from being born again.
"""
from __future__ import annotations

import inspect

from app.agents.competitor_research import agent as competitor_agent
from app.api.v1 import data as data_api
from app.models import competitor as competitor_model
from app.models.competitor import Competitor
from app.schemas import agent_outputs


def _live_paths() -> set[str]:
    from app.main import app

    return set(app.openapi()["paths"])


def test_no_route_reads_the_competitors_table() -> None:
    paths = _live_paths()
    assert [path for path in paths if "competitor" in path.lower()] == [], (
        "a competitor route landed — update the model/agent claims and close R-W5-CA-1"
    )
    assert [path for path in paths if "{table}" in path] == [], (
        "a generic /data/{table} dump landed — the claims that say it does not exist must change"
    )
    assert {path for path in paths if "/data" in path} == {"/api/v1/data", "/api/v1/data/export"}, (
        f"the /data surface changed: {sorted(path for path in paths if '/data' in path)}"
    )


def test_the_table_is_wiped_but_never_exported() -> None:
    assert data_api.MEMBER_MODELS["competitors"] is Competitor, (
        "competitors must stay a wipe member (its rows are owner data and are deleted)"
    )
    assert "competitors" not in data_api.EXPORT_MODELS, (
        "the export ships 8 fixed members; competitors is not one of them"
    )


def test_the_worker_has_no_competitors_writer() -> None:
    """The producer half of R-W5-CA-1 is still absent — the refusal is load-bearing."""
    from celery_app.tasks import agent_tasks

    source = inspect.getsource(agent_tasks)
    writing = [
        f"{name}(" for name in ("Competitor", "competitor_service")
        if name in source
    ]
    assert writing == [], (
        f"a competitors writer landed in the worker ({writing}); a write-only table is exactly what "
        "R-W5-CA-1 refuses — land the consumer in the same change and update this pin"
    )
    # The agent TYPE is still referenced for its budget branch; that is not a table writer.
    assert 'agent_type == "competitor_research"' in source


def test_the_three_claims_name_the_refusal_and_not_a_phantom_reader() -> None:
    """The claim correction is part of the fix: a doc that names a non-existent route is drift."""
    texts = {
        "models/competitor.py": competitor_model.__doc__ or "",
        "schemas/agent_outputs.py": agent_outputs.__doc__ or "",
        "agents/competitor_research/agent.py": competitor_agent.__doc__ or "",
    }
    for name, text in texts.items():
        lowered = text.lower()
        assert "R-W5-CA-1" in text, f"{name} must name the request that carries the decision"
        assert "no writer" in lowered or "not written" in lowered, (
            f"{name} must state that the table is not written"
        )
        assert "no reader" in lowered or "nothing reads" in lowered, (
            f"{name} must state that nothing reads the table"
        )
        # Quoting the old wrong claim is allowed ONLY with its refutation beside it.
        if "/data/{table}" in text:
            assert "does not exist" in text, (
                f"{name} names the phantom generic dump without refuting it"
            )
    # The wipe reference is the one TRUE consumer-ish reference and is named where it matters.
    assert "MEMBER_MODELS" in texts["models/competitor.py"]
