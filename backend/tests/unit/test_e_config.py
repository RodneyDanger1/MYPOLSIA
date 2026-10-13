"""T4 + T14 — the malformed-`COMPANY_TIMEZONE` refusal, its 503 mapping, and the dead RATE_CAPPED rows.

T4 (seat S2-t4t14): `timeutil.company_tz` converts W3-refusals' S-04 warning into the named
`E_CONFIG` refusal.  Unset stays the DOCUMENTED UTC default (`.env.example:50` ships
`COMPANY_TIMEZONE=UTC`); a non-empty non-zone is never answered with a plausible value.
The other `E_CONFIG` raise site (T2's morning-hour validation) is re-pinned here and by
`test_daily_cycle_window.py`.

T14: `FailureCode.RATE_CAPPED` ("POLSIA-E117") is dead in the CLI-envelope taxonomy —
`grep -rn RATE_CAPPED backend` found only the enum member and its two mapping rows; the cap
is raised by the policy firewall as the raw "POLSIA-E117" string (`policy_firewall.py:415`,
via `PolicyDecision.e_code`), never by `classify_error_envelope`.  The two rows are deleted;
the member stays (stable numbering + the comment that explains its real runtime).
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from app.agents.structured_output import FAILURE_TO_E_CODE, REMEDIATION, FailureCode, e_code_for
from app.core import timeutil
from app.core.errors import (
    E_AGENT_BAD_OUTPUT,
    E_CONFIG,
    E_REDIS,
    HTTP_STATUS_BY_CODE,
    PolsiaError,
    http_status_for,
)

# ── T4 — company_tz: malformed = refusal, unset = documented default ──────────


def test_malformed_company_timezone_is_a_named_e_config_refusal(monkeypatch):
    """`"Not/AZone"` raises `PolsiaError(E_CONFIG)`; the message names the bad value.

    The raise IS the "no silent UTC" proof: no fallback value ever reaches a caller, and
    the refusal text must not offer a quiet default either.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "company_timezone", "Not/AZone")

    with pytest.raises(PolsiaError) as excinfo:
        timeutil.company_tz()

    err = excinfo.value
    assert err.code == E_CONFIG
    assert "Not/AZone" in err.message, "the refusal must name the value that is wrong"
    assert "UTC" not in err.message, "no quiet default hides in the refusal text"
    assert err.how_to_fix and "America/Chicago" in err.how_to_fix, (
        "how_to_fix names an IANA example"
    )
    assert err.context == {"value": "Not/AZone", "expected": "IANA timezone name"}


def test_malformed_explicit_argument_raises_the_same_refusal():
    """An explicit `tz_name` bypassing settings is the same state — refusal, never a fallback."""
    with pytest.raises(PolsiaError) as excinfo:
        timeutil.company_tz("Not/AZone")

    assert excinfo.value.code == E_CONFIG
    assert "Not/AZone" in excinfo.value.message


def test_unset_company_timezone_is_the_documented_utc_default(monkeypatch):
    """Both falsy spellings (`""` setting, `""` argument) answer UTC — no raise, no record."""
    from app.config import settings

    monkeypatch.setattr(settings, "company_timezone", "")

    assert timeutil.company_tz() == ZoneInfo("UTC")
    assert timeutil.company_tz("").key == "UTC"


def test_valid_zone_behaviour_is_unchanged(monkeypatch):
    """A valid zone is returned as-is and `zoneinfo` caches it — same object on each call."""
    from app.config import settings

    monkeypatch.setattr(settings, "company_timezone", "Asia/Tokyo")

    tz = timeutil.company_tz()
    assert tz == ZoneInfo("Asia/Tokyo") and tz.key == "Asia/Tokyo"
    assert timeutil.company_tz() is tz, "the zone lookup itself is unchanged"


def test_e_config_http_status_is_503_like_e_redis():
    """Same class as `E_REDIS`: the service's own (config) outage, reachable at request time."""
    assert http_status_for(E_CONFIG) == 503
    assert HTTP_STATUS_BY_CODE[E_CONFIG] == 503
    assert http_status_for(E_CONFIG) == http_status_for(E_REDIS)


@pytest.mark.asyncio
async def test_morning_hour_refusal_second_site_still_e_config(monkeypatch):
    """T2's other `E_CONFIG` site is unmoved: a stored hour outside 0–23 refuses, naming it."""
    from app.services import company_service
    from celery_app.tasks import daily_cycle

    async def _stored_hour(_db):
        return 25

    monkeypatch.setattr(company_service, "get_daily_cycle_hour", _stored_hour)

    with pytest.raises(PolsiaError) as excinfo:
        await daily_cycle._resolve_morning_hour(None)

    assert excinfo.value.code == E_CONFIG
    assert "25" in excinfo.value.message


# ── T14 — the dead RATE_CAPPED rows ───────────────────────────────────────────


def test_t14_rate_capped_member_survives_without_its_rows():
    """The enum member stays (stable POLSIA-E117 naming); the two mapping rows are gone."""
    assert FailureCode.RATE_CAPPED.value == "POLSIA-E117"
    assert FailureCode.RATE_CAPPED not in FAILURE_TO_E_CODE
    assert FailureCode.RATE_CAPPED not in REMEDIATION
    # the lookup cannot KeyError for the member in any reachable path: `.get(...) or` guards it
    assert e_code_for(FailureCode.RATE_CAPPED) == E_AGENT_BAD_OUTPUT


def test_t14_the_two_maps_agree_on_the_classified_subset():
    """Every member carries a decision in both maps — except `OK` (success) and the dead member.

    PLAN CORRECTION (the T14 brief asked for `set(FAILURE_TO_E_CODE) == set(REMEDIATION)`):
    those sets can never be equal — `FAILURE_TO_E_CODE` documents the success member `OK`
    mapped to `""` (no error code, hence no remediation text) — so the honest invariant is
    the classified-subset agreement below.  It fails loudly if either map drifts from the
    census: a new member must get its rows, and no dead row may linger.
    """
    assert set(FAILURE_TO_E_CODE) == set(REMEDIATION) | {FailureCode.OK}
    assert set(FailureCode) - set(FAILURE_TO_E_CODE) == {FailureCode.RATE_CAPPED}
    assert set(FailureCode) - set(REMEDIATION) == {FailureCode.OK, FailureCode.RATE_CAPPED}
    assert all(isinstance(key, FailureCode) for key in FAILURE_TO_E_CODE)
    assert all(isinstance(key, FailureCode) for key in REMEDIATION)


def test_e_config_status_maps_agree_where_they_overlap():
    """The two status tables must not drift (parent-surgical completion of T4).

    `deps.as_http_exception`/`refusal_status` and `errors.to_http_detail`/`http_status_for`
    both render refusals; the same refusal must not get two statuses depending on which
    boundary caught it.  `deps.STATUS_BY_CODE` is the richer table (it owns the CLI/storage
    rows); wherever the two overlap they must say the same thing, and `E_CONFIG` must be
    503 in both.
    """
    from app.api import deps

    shared = set(HTTP_STATUS_BY_CODE) & set(deps.STATUS_BY_CODE)
    assert shared, "the tables must overlap or this guard is vacuous"
    mismatched = {
        key: (HTTP_STATUS_BY_CODE[key], deps.STATUS_BY_CODE[key])
        for key in shared
        if HTTP_STATUS_BY_CODE[key] != deps.STATUS_BY_CODE[key]
    }
    assert not mismatched, f"status tables disagree on {mismatched}"
    assert deps.STATUS_BY_CODE[E_CONFIG] == 503
