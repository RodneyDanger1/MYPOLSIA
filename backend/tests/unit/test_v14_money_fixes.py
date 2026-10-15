"""V14/V1 — the money-surface fix wave: S4-F1, S4-F2, S4-F3, S1-04 (RED-first legs).

Every leg in this file was run RED against the pre-fix tree (lane `raw/red.txt`) and GREEN
after the fixes (`raw/green-focus.txt`):

* **S4-F1** — `POST /api/v1/invoices` accepted `NaN`/`Infinity` money, COMMITTED the row,
  then 500'd at response render; from then on every `GET /api/v1/invoices` answered an
  uncoded 500 (wave-v13/s4-errors FINDINGS F1; probes/probe_nan_chain.py).  Fixed at three
  layers: the wire models (`allow_inf_nan=False`), `finance_service.money()` (named
  refusal), and the response boundary (a dirty row refuses BY NAME — never a silent `null`
  laundering, never a whole-collection 500).
* **S4-F2** — a body carrying a `NaN` literal made the 422 responder ITSELF raise
  (`json.dumps(..., allow_nan=False)` on pydantic's echoed input) -> uncoded 500.  Fixed
  by ONE `RequestValidationError` handler that keeps the framework shape for serializable
  error sets (still pinned "uncoded by design") and emits a named `E_VALIDATION` envelope
  for the non-finite set that used to crash.
* **S4-F3** — int64-overflowing path/query ints reached the SQLite driver ->
  `OverflowError: Python int too large to convert to SQLite INTEGER` -> uncoded
  `500 text/plain`.  Fixed by a named 422 at the handler seam (+ an explicit upper bound
  on the invoice id this seat owns).
* **S1-04** — `invoices.tax` carried `default=0` + `server_default '0'`, so an unmeasured
  tax was STORED as a measured 0 (wave-v13/s1-backend FINDINGS S1-04).  Fixed in the model
  + migration 0009; the pin that missed it is strengthened in `test_v9_invoices.py`.

Pins that CONSTRAIN the S4-F2 shape (a blanket "unify the 422 shape" would break them):
`tests/unit/test_refusal_unify.py:146-151` and `tests/unit/api/test_r26_ads_edges.py:196-204`.

Fixtures: the real app (`api_client`, conftest) for the wire legs; the migration-head
fixture from `tests/unit/test_r26_ads_sweep.py` (imported for registration, same pattern as
`tests/unit/api/test_r26_ads_edges.py`) for the S1-04 DDL leg.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sqltext
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.errors import E_DB, E_VALIDATION, PolsiaError
from app.models.invoice import Invoice, InvoiceStatus
from tests.unit.test_r26_ads_sweep import head_db  # noqa: F401  (fixture registration)

AUTH = {"X-API-Key": "test-key"}
RAW_JSON = {**AUTH, "content-type": "application/json"}
ISSUE = date(2026, 11, 1)
DUE = ISSUE + timedelta(days=30)
BIG = 2**63  # one past int64 — the exact value wave-v13/s4-errors probed


async def _count(db) -> int:
    return (await db.execute(select(func.count()).select_from(Invoice))).scalar_one()


async def _post_raw(client, url: str, body: str):
    """POST a RAW JSON text body — `NaN`/`Infinity` literals are not spec JSON, so they
    cannot be sent through a serializer; the poison payload goes on the wire verbatim."""
    return await client.post(url, content=body.encode(), headers=RAW_JSON)


# ── S4-F1: non-finite money ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
async def test_nonfinite_money_is_refused_at_the_wire_and_persists_nothing(
    api_client, async_db_session, token
):
    """The F1 chain, inverted: refused BY NAME, zero rows written, the list stays 200."""
    before = await _count(async_db_session)

    resp = await _post_raw(
        api_client,
        "/api/v1/invoices",
        f'{{"subtotal": {token}, "tax": {token}, "total": {token}}}',
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_VALIDATION
    assert body["error"]["how_to_fix"]
    assert resp.headers.get("X-Polsia-Error-Code") == E_VALIDATION
    # the offending field is named and the non-finite echo is sanitized (no raw NaN on the wire)
    echo_by_token = {"NaN": "nan", "Infinity": "inf", "-Infinity": "-inf"}
    money_errors = [e for e in body["detail"] if "subtotal" in e.get("loc", [])]
    assert money_errors and all(e["input"] == echo_by_token[token] for e in money_errors)

    assert await _count(async_db_session) == before, "a refused create must not persist a row"
    listed = await api_client.get("/api/v1/invoices", headers=AUTH)
    assert listed.status_code == 200, listed.text
    assert listed.json() == []


@pytest.mark.asyncio
async def test_money_guard_refuses_non_finite_and_passes_the_rest_through():
    """`money()` is the service-layer belt: non-finite refuses by name; None/Decimal/str
    keep their existing exact-decimal behaviour (no over-refusal)."""
    from app.services.finance_service import money

    for bad in (float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(PolsiaError) as ei:
            money(bad)
        assert ei.value.code == E_VALIDATION
        assert ei.value.how_to_fix

    assert money(None) is None
    assert money(1.5) == Decimal("1.5")
    assert money("12.5") == Decimal("12.5")
    assert money(Decimal("0.10")) == Decimal("0.10")


@pytest.mark.asyncio
async def test_a_dirty_row_refuses_by_name_instead_of_poisoning_the_list(
    api_client, async_db_session
):
    """Damage that PREDATES the boundary fix (an import, a hand-edited DB): the row is
    refused BY NAME (E_DB, naming row + columns) — the whole collection must not 500, and
    the stored NaN must never be silently laundered into a null "not measured"."""
    clean = Invoice(
        number="INV-2026-900", status=InvoiceStatus.DRAFT, issue_date=ISSUE, due_date=DUE,
        net_days=30, currency="USD", subtotal=Decimal("100"),
    )
    dirty = Invoice(
        number="INV-2026-901", status=InvoiceStatus.DRAFT, issue_date=ISSUE, due_date=DUE,
        net_days=30, currency="USD", total=Decimal("Infinity"),
    )
    async_db_session.add_all([clean, dirty])
    await async_db_session.commit()
    dirty_id = dirty.id
    async_db_session.expunge_all()

    listed = await api_client.get("/api/v1/invoices", headers=AUTH)
    assert listed.status_code == 503, listed.text
    body = listed.json()
    assert body["error"]["code"] == E_DB
    assert "INV-2026-901" in body["error"]["message"]
    assert body["error"]["context"]["invoice_id"] == dirty_id
    assert listed.headers.get("X-Polsia-Error-Code") == E_DB

    one = await api_client.get(f"/api/v1/invoices/{dirty_id}", headers=AUTH)
    assert one.status_code == 503 and one.json()["error"]["code"] == E_DB

    # a clean row next to the dirty one still renders — the refusal is per-row, not per-table
    clean_id = clean.id
    ok = await api_client.get(f"/api/v1/invoices/{clean_id}", headers=AUTH)
    assert ok.status_code == 200, ok.text
    assert ok.json()["subtotal"] == 100.0


# ── S4-F2: NaN in ANY body must not kill the 422 responder ───────────────────────────────────


@pytest.mark.asyncio
async def test_nan_in_body_gets_a_named_422(api_client):
    resp = await _post_raw(api_client, "/api/v1/tasks", '{"title": NaN}')
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_VALIDATION
    assert resp.headers.get("X-Polsia-Error-Code") == E_VALIDATION
    # the sanitized pydantic array survives: the field is named, the echo is a string
    named = [e for e in body["detail"] if e.get("loc") == ["body", "title"]]
    assert named and named[0]["input"] == "nan"


@pytest.mark.asyncio
async def test_nan_hour_in_schedule_body_gets_the_same_named_422(api_client):
    resp = await _post_raw(api_client, "/api/v1/schedules", '{"hour": NaN}')
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == E_VALIDATION


@pytest.mark.asyncio
async def test_ordinary_validation_errors_keep_the_uncoded_default_shape(api_client):
    """The conditional's OTHER side — the shipped pins that must stay byte-shaped:
    refusal_unify:146-151 (detail array, no `ok`) and r26_ads_edges:196-204 (`error` absent
    from framework 422s, no invented code header)."""
    resp = await api_client.get("/api/v1/ads/campaigns", headers=AUTH, params={"limit": "0"})
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"detail"} and body["detail"][0]["type"] == "greater_than_equal"
    assert resp.headers.get("X-Polsia-Error-Code") is None

    resp2 = await api_client.post("/api/v1/tasks", headers=AUTH, json={"nothing": "here"})
    assert resp2.status_code == 422
    assert "ok" not in resp2.json() and isinstance(resp2.json()["detail"], list)


# ── S4-F3: int64 overflow ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,url",
    [
        ("get", f"/api/v1/tasks/{BIG}"),
        ("get", f"/api/v1/tasks?offset={BIG}"),
        ("delete", f"/api/v1/memory/{BIG}"),
    ],
)
async def test_int64_overflow_is_a_named_422_never_a_500(api_client, method, url):
    resp = await getattr(api_client, method)(url, headers=AUTH)
    assert resp.status_code == 422, f"{url}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == E_VALIDATION
    assert resp.headers.get("X-Polsia-Error-Code") == E_VALIDATION
    assert "9223372036854775807" in body["error"]["how_to_fix"]


@pytest.mark.asyncio
async def test_the_boundary_is_exactly_the_64_bit_edge(api_client):
    """Everything <= int64 is business as usual — only the overflow is refused."""
    inside = await api_client.get(f"/api/v1/tasks/{BIG - 1}", headers=AUTH)
    assert inside.status_code == 404, inside.text
    assert inside.json()["error"]["code"] == "E_NOT_FOUND"


@pytest.mark.asyncio
async def test_the_invoice_id_this_seat_owns_is_bounded_before_the_db(api_client):
    """The invoices route bounds at the wire (pydantic 422, field named) — belt + braces
    next to the global handler."""
    resp = await api_client.get(f"/api/v1/invoices/{BIG}", headers=AUTH)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["path", "invoice_id"]


# ── S1-04: `tax: None` stays NULL ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orm_default_does_not_fabricate_tax(async_db_session):
    """An invoice created without a tax stores NULL; an EXPLICIT measured zero survives."""
    inv = Invoice(
        number="INV-2026-901", status=InvoiceStatus.DRAFT, issue_date=ISSUE, due_date=DUE,
        net_days=30, currency="USD",
    )
    async_db_session.add(inv)
    await async_db_session.commit()
    async_db_session.expunge_all()  # force a real read-back, not the in-memory attribute
    row = (
        await async_db_session.execute(select(Invoice).where(Invoice.number == "INV-2026-901"))
    ).scalar_one()
    assert row.tax is None, f"unmeasured tax must stay NULL, got {row.tax!r}"

    zero = Invoice(
        number="INV-2026-902", status=InvoiceStatus.DRAFT, issue_date=ISSUE, due_date=DUE,
        net_days=30, currency="USD", tax=Decimal("0"),
    )
    async_db_session.add(zero)
    await async_db_session.commit()
    async_db_session.expunge_all()
    row2 = (
        await async_db_session.execute(select(Invoice).where(Invoice.number == "INV-2026-902"))
    ).scalar_one()
    assert row2.tax == Decimal("0"), "an EXPLICIT measured zero must survive"


@pytest.mark.asyncio
async def test_migration_head_ddl_does_not_fabricate_tax(request):
    """The DDL half of S1-04 (migration 0009): a raw INSERT omitting `tax` on a
    migration-head database must store NULL, not the fabricated 0 server default."""
    _db_path, url = request.getfixturevalue("head_db")
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as session:
            await session.execute(
                sqltext(
                    "INSERT INTO invoices (number, issue_date, due_date, net_days, currency, status) "
                    "VALUES ('INV-2026-950', '2026-10-15', '2026-11-14', 30, 'USD', 'draft')"
                )
            )
            await session.commit()
            val = (
                await session.execute(
                    sqltext("SELECT tax FROM invoices WHERE number = 'INV-2026-950'")
                )
            ).scalar_one()
        assert val is None, f"migration-head INSERT omitting tax stored {val!r}"
    finally:
        await engine.dispose()
