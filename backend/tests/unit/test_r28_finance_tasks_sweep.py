"""R28 — the finance webhook ladder + the query_helpers refusal plumbing, exercised directly.

Read-verified anchors (HEAD 9927fca): `_parse_signature_header` `finance.py:102-111`,
`_stdlib_verify` `:114-158`, `construct_stripe_event` `:161-185`, `_signature_tolerance` `:188-189`;
`db_refusal` `query_helpers.py:23-30`, `fetch_rows` `:33-38`, `fetch_scalars` `:41-46`,
`fetch_scalar` `:49-54`, `fetch_one` `:57-67`, `agent_rollup_expr` `:70-81`; `_split_agents`
`dashboard.py:82-85`.

Every signature below is built with stdlib HMAC over `"{t}.{raw_body}"` — the same construction
the code verifies — so no live Stripe call and no invented header shape is involved. `stripe`
11.3.0 IS installed in this venv (`raw/probe_stripe_presence.txt`), so the route's default path is
the SDK branch; the documented no-SDK fallback (the whole `_stdlib_verify` ladder) is reached here
by forcing the absent-import state, which is exactly what a host without the SDK produces.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import sys
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import MultipleResultsFound, SQLAlchemyError

from app.api.v1.dashboard import _split_agents
from app.api.v1.finance import (
    DEFAULT_SIGNATURE_TOLERANCE,
    _parse_signature_header,
    _signature_tolerance,
    _stdlib_verify,
    construct_stripe_event,
)
from app.core.errors import E_DB, ERROR_HINTS, PolsiaError
from app.core.timeutil import dialect_name
from app.models.task import Task
from app.services.query_helpers import (
    agent_rollup_expr,
    db_refusal,
    fetch_one,
    fetch_rows,
    fetch_scalar,
    fetch_scalars,
)

SECRET = "whsec_r28_sweep"


def _sign(body: bytes, *, secret: str = SECRET, ts: int | None = None) -> str:
    """The documented header: HMAC-SHA256 over `"<t>.<raw body>"`, exactly as the code verifies."""
    stamp = int(time.time()) if ts is None else ts
    digest = hmac.new(
        secret.encode("utf-8"), f"{stamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={stamp},v1={digest}"


def _refusal(call, needle: str) -> HTTPException:
    """Assert a 400 whose message names the failure (the ladder's whole contract), return it."""
    with pytest.raises(HTTPException) as excinfo:
        call()
    assert excinfo.value.status_code == 400, excinfo.value.detail
    assert needle in str(excinfo.value.detail), excinfo.value.detail
    return excinfo.value


def _broken_select():
    """A valid SELECT on a table that does not exist: a real driver failure, not a mock."""
    return select(func.count()).select_from(text("r28_missing_table"))


# ── 1. the signature header parser ───────────────────────────────────────────────


def test_parse_signature_header_shapes() -> None:
    assert _parse_signature_header("t=123,v1=abc") == ("123", "abc")
    assert _parse_signature_header("t=1, v1=deadbeef") == ("1", "deadbeef")  # spaces around parts
    assert _parse_signature_header("scheme=x,t=2,v1=aa") == ("2", "aa")  # unknown parts ignored
    # Multiple v1 values (secret rotation): the LAST one wins and is the only one tried.  That is
    # a documented divergence from the SDK, which accepts ANY of them — REQUESTS #5, pinned here.
    assert _parse_signature_header("t=1,v1=first,v1=second") == ("1", "second")
    assert _parse_signature_header("t=1,t=9,v1=a") == ("9", "a")  # duplicate t: last wins
    assert _parse_signature_header("T=1,V1=abc") == (None, None)  # keys are case-sensitive
    assert _parse_signature_header("v1") == (None, "")  # a bare part carries no v1 value
    assert _parse_signature_header("") == (None, None)


def test_stdlib_verify_accepts_a_really_signed_body() -> None:
    body = json.dumps({"id": "evt_ok", "type": "charge.succeeded"}).encode("utf-8")
    payload = _stdlib_verify(body, _sign(body), SECRET, DEFAULT_SIGNATURE_TOLERANCE)
    assert payload == {"id": "evt_ok", "type": "charge.succeeded"}


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("", id="empty-header"),
        pytest.param("v1=abc", id="no-timestamp"),
        pytest.param("t=12345", id="no-signature"),
        pytest.param("t=12345,s1=abc", id="wrong-scheme"),
    ],
)
def test_stdlib_verify_refuses_a_header_without_a_t_and_v1_pair(header: str) -> None:
    _refusal(
        lambda: _stdlib_verify(b"{}", header, SECRET, DEFAULT_SIGNATURE_TOLERANCE),
        "Invalid Stripe signature header",
    )


def test_stdlib_verify_refuses_a_non_numeric_timestamp() -> None:
    _refusal(
        lambda: _stdlib_verify(b"{}", "t=notanint,v1=abc", SECRET, DEFAULT_SIGNATURE_TOLERANCE),
        "Invalid Stripe signature timestamp",
    )


def test_stdlib_verify_refuses_a_wrong_signature() -> None:
    now = int(time.time())
    _refusal(
        lambda: _stdlib_verify(b"{}", f"t={now},v1=deadbeef", SECRET, DEFAULT_SIGNATURE_TOLERANCE),
        "Invalid Stripe signature",
    )


def test_stdlib_verify_refuses_stale_and_future_timestamps() -> None:
    body = b'{"id": "evt_aged"}'
    now = int(time.time())
    _refusal(
        lambda: _stdlib_verify(body, _sign(body, ts=now - 3600), SECRET, DEFAULT_SIGNATURE_TOLERANCE),
        f"outside the {DEFAULT_SIGNATURE_TOLERANCE}s tolerance",
    )
    _refusal(
        lambda: _stdlib_verify(body, _sign(body, ts=now + 3600), SECRET, DEFAULT_SIGNATURE_TOLERANCE),
        f"outside the {DEFAULT_SIGNATURE_TOLERANCE}s tolerance",
    )


def test_stdlib_verify_refuses_a_valid_signature_for_a_different_body() -> None:
    """A captured (signature, timestamp) pair does not authenticate a swapped body."""
    signed_body = b'{"id": "evt_a", "type": "charge.succeeded"}'
    other_body = b'{"id": "evt_b", "type": "charge.refunded"}'
    _refusal(
        lambda: _stdlib_verify(
            other_body, _sign(signed_body), SECRET, DEFAULT_SIGNATURE_TOLERANCE
        ),
        "Invalid Stripe signature",
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"\xff\xfe\x00", id="non-utf8-bytes"),
        pytest.param(b"not json at all", id="not-json"),
    ],
)
def test_stdlib_verify_refuses_a_signed_body_that_is_not_json(body: bytes) -> None:
    _refusal(
        lambda: _stdlib_verify(body, _sign(body), SECRET, DEFAULT_SIGNATURE_TOLERANCE),
        "Invalid JSON body",
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"[1, 2, 3]", id="array"),
        pytest.param(b'"just a string"', id="string"),
        pytest.param(b"7", id="number"),
        pytest.param(b"null", id="null"),
    ],
)
def test_stdlib_verify_refuses_signed_json_that_is_not_an_object(body: bytes) -> None:
    _refusal(
        lambda: _stdlib_verify(body, _sign(body), SECRET, DEFAULT_SIGNATURE_TOLERANCE),
        "Invalid event payload",
    )


def test_stdlib_verify_tolerance_zero_disables_the_window() -> None:
    """`tolerance=0` skips the age check — the documented escape hatch for clock-skewed hosts."""
    body = b'{"id": "evt_old_but_allowed"}'
    header = _sign(body, ts=int(time.time()) - 100_000)
    assert _stdlib_verify(body, header, SECRET, 0)["id"] == "evt_old_but_allowed"


# ── 2. construct_stripe_event: window default, coercion, SDK-absent fallback ─────


def test_construct_stripe_event_defaults_the_window_to_the_300s_tolerance() -> None:
    body = b'{"id": "evt_window"}'
    header = _sign(body, ts=int(time.time()) - 4000)  # correctly signed, older than the default
    with pytest.raises(HTTPException) as excinfo:
        construct_stripe_event(body, header, SECRET)
    assert excinfo.value.status_code == 400
    # The caller can widen or shrink the window explicitly; a string is coerced by int().
    assert construct_stripe_event(body, header, SECRET, tolerance=10_000)["id"] == "evt_window"
    assert construct_stripe_event(body, header, SECRET, tolerance="6000")["id"] == "evt_window"


def test_construct_stripe_event_falls_back_to_the_stdlib_check_when_the_sdk_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented "host without stripe" production path: `import stripe` raises ImportError."""
    monkeypatch.setitem(sys.modules, "stripe", None)
    body = b'{"id": "evt_fallback", "type": "charge.succeeded"}'
    assert construct_stripe_event(body, _sign(body), SECRET)["id"] == "evt_fallback"
    _refusal(
        lambda: construct_stripe_event(body, _sign(body, secret="whsec_other"), SECRET),
        "Invalid Stripe signature",
    )


def test_signature_tolerance_reads_settings_with_a_300s_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    assert DEFAULT_SIGNATURE_TOLERANCE == 300
    # The frozen settings surface (tests/parity FROZEN_SETTINGS) declares no override field, so
    # the default is what production runs with.  A pydantic Settings model refuses setattr for an
    # undeclared field, so the "configured value" state is staged in the instance dict — the same
    # place a declared field's value lives — and the getattr hook is what must honour it.
    assert _signature_tolerance() == 300
    monkeypatch.setitem(settings.__dict__, "stripe_signature_tolerance", "45")
    assert _signature_tolerance() == 45


def test_the_docstring_no_longer_claims_the_sdk_is_absent() -> None:
    """The module docstring shipped a false measured claim ("not installed in this venv") while
    `stripe` 11.3.0 IS installed (raw/probe_stripe_presence.txt).  A docstring that contradicts
    the measured environment is the same class of lie the repo refuses in code paths."""
    import app.api.v1.finance as finance_module

    source = inspect.getsource(finance_module)
    assert "not installed in this venv" not in source


# ── 3. query_helpers: the named refusal + every fetch_* shape ────────────────────


def test_db_refusal_shape_names_the_query_and_the_hint() -> None:
    err = db_refusal("widgets", ValueError("boom"))
    assert isinstance(err, PolsiaError)
    assert err.code == E_DB
    assert err.message == "could not read widgets: ValueError: boom"
    assert err.how_to_fix == ERROR_HINTS[E_DB]
    assert err.how_to_fix, "the hint must be reachable, never empty"
    assert err.context == {"query": "widgets"}
    assert str(err).startswith(f"{E_DB}: could not read widgets")


async def _seed_two_tasks(db) -> None:
    db.add_all(
        [
            Task(title="r28-a", agent_type="finance"),
            Task(title="r28-b", agent_type="social_media"),
        ]
    )
    await db.commit()


@pytest.mark.asyncio
async def test_fetch_rows_returns_rows_and_refuses_a_driver_failure(async_db_session) -> None:
    await _seed_two_tasks(async_db_session)
    rows = await fetch_rows(
        async_db_session, select(Task.title, Task.agent_type).order_by(Task.id), what="two tasks"
    )
    assert [title for title, _agent in rows] == ["r28-a", "r28-b"]

    with pytest.raises(PolsiaError) as excinfo:
        await fetch_rows(async_db_session, _broken_select(), what="tasks probe")
    assert excinfo.value.code == E_DB
    assert excinfo.value.context == {"query": "tasks probe"}
    assert "could not read tasks probe" in excinfo.value.message
    assert isinstance(excinfo.value.__cause__, SQLAlchemyError), "the driver error is chained"


@pytest.mark.asyncio
async def test_fetch_scalars_returns_values_and_refuses_a_driver_failure(async_db_session) -> None:
    await _seed_two_tasks(async_db_session)
    titles = await fetch_scalars(
        async_db_session, select(Task.title).order_by(Task.title), what="task titles"
    )
    assert titles == ["r28-a", "r28-b"]

    with pytest.raises(PolsiaError) as excinfo:
        await fetch_scalars(async_db_session, _broken_select(), what="scalars probe")
    assert excinfo.value.code == E_DB and excinfo.value.context == {"query": "scalars probe"}


@pytest.mark.asyncio
async def test_fetch_scalar_returns_the_first_value_and_refuses_a_driver_failure(
    async_db_session,
) -> None:
    await _seed_two_tasks(async_db_session)
    count = await fetch_scalar(
        async_db_session, select(func.count()).select_from(Task), what="task count"
    )
    assert count == 2

    with pytest.raises(PolsiaError) as excinfo:
        await fetch_scalar(async_db_session, _broken_select(), what="scalar probe")
    assert excinfo.value.code == E_DB and excinfo.value.context == {"query": "scalar probe"}


@pytest.mark.asyncio
async def test_fetch_one_returns_none_when_empty_and_refuses_multiple_rows(async_db_session) -> None:
    assert (
        await fetch_one(async_db_session, select(Task).where(Task.id == 1), what="one task")
        is None
    )
    await _seed_two_tasks(async_db_session)
    # `fetch_one` returns the ROW (a `one_or_none()` result); a single-entity select yields a
    # one-element row, so the Task is row[0] — the same contract dashboard.py:91-112 reads by name.
    one = await fetch_one(async_db_session, select(Task).where(Task.title == "r28-a"), what="one task")
    assert one is not None and one[0].title == "r28-a"

    # "a query that promised one row and returned several is a bug worth seeing": re-raised as a
    # named E_DB with the ORIGINAL MultipleResultsFound chained, never silently truncated.
    with pytest.raises(PolsiaError) as excinfo:
        await fetch_one(async_db_session, select(Task).order_by(Task.id).limit(2), what="one task")
    assert excinfo.value.code == E_DB
    assert isinstance(excinfo.value.__cause__, MultipleResultsFound)

    with pytest.raises(PolsiaError) as excinfo:
        await fetch_one(async_db_session, _broken_select(), what="one row probe")
    assert excinfo.value.code == E_DB and excinfo.value.context == {"query": "one row probe"}


# ── 4. the dialect choice ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_rollup_expr_picks_each_engines_aggregate(async_db_session) -> None:
    assert dialect_name(async_db_session) == "sqlite", "the unit DB is the sqlite dialect path"
    await _seed_two_tasks(async_db_session)
    async_db_session.add(Task(title="r28-c", agent_type="finance"))  # duplicate agent type
    await async_db_session.commit()

    rows = await fetch_scalars(
        async_db_session,
        select(agent_rollup_expr("sqlite", Task.agent_type)).select_from(Task),
        what="agent rollup",
    )
    assert len(rows) == 1
    assert set(rows[0].split(",")) == {"finance", "social_media"}
    assert rows[0].count("finance") == 1, "group_concat(DISTINCT ...) must not repeat an agent"

    pg_sql = str(
        select(agent_rollup_expr("postgresql", Task.agent_type)).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "string_agg" in pg_sql and "DISTINCT" in pg_sql
    # Any other engine string falls back to the sqlite spelling (it is the portable default).
    fallback_sql = str(
        select(agent_rollup_expr("mysql", Task.agent_type)).compile(dialect=postgresql.dialect())
    )
    assert "group_concat" in fallback_sql


# ── 5. the dashboard rollup parser ───────────────────────────────────────────────


def test_split_agents_parses_the_rollup_or_stays_empty() -> None:
    assert _split_agents(None) == []
    assert _split_agents("") == []
    assert _split_agents("finance") == ["finance"]
    assert _split_agents("b,a,a") == ["a", "b"], "sorted + deduped"
    assert _split_agents("a,,b") == ["a", "b"], "empty parts are dropped, not kept as agents"
