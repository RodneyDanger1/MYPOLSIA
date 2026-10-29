"""B1 / D6 — the A3 blast radius, pinned where it is durable.

Wave A3 landed eight commits that added a route, three error codes, two egress symbols and
several registry exports, and it moved the shared enqueue hand-off from "write `failed` on every
row" to "re-arm the rows".  The wave's own receipts prove the mechanics; this file proves the
things that would drift *silently* afterwards:

  1. **Pin families.** A new route/code/symbol is not "added" until every list that publishes the
     surface carries it.  These tests read the same pin lists the gate reads
     (`tests/parity/*`, the R29 status census, each module's `__all__`) and fail when one lags.
  2. **The claims that are true and must stay true.** The egress fence refuses the addresses an
     SSRF steers at; `web_fetch` has ONE resolver; the replay refusal is one code object.  Each is
     asserted at the seam the defect lived in, not through a mock of the fix.
  3. **The known-unfixed classes, named.** Every `xfail` below carries the finding/request id, the
     engine reason it cannot pass yet, and the command that settles it once the fix lands.  They
     are `strict=False` on purpose: another seat landing the fix turns them into XPASS (visible in
     the junit XML), never a red gate for work that was done.
  4. **The order-dependence class A3 fixed.** `app.dependency_overrides[get_db] = …` on the shared
     app object outlives the test that wrote it; A3 repaired one instance by hand
     (`test_a3_state.py`'s `monkeypatch.setitem`).  This file pins the RULE over every test module,
     so the next bare assignment is caught at review time instead of in a random full-gate order.

Measured against `HEAD = 93a0fd5` (the A3 settle commit).  No network, no broker, no server: the
DoH answer in `test_a_do_h_answer…` is stubbed at `trust_service._doh`, the one named seam.

B1 phase B (seat C6) CLOSED two of the carries pinned here — the pricing alias (F-19 / S6-R3) and
the campaign money defaults (D4-R1 / A2A7-01's schema half).  Their `xfail` markers are gone and
the assertions are plain, so a regression in either is a RED test now, not an XPASS nobody reads.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2]          # backend/
REPO = BACKEND.parent                                            # repo root


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 1. PIN FAMILIES — the new surface is in every list that publishes it
# ═══════════════════════════════════════════════════════════════════════════════════════════════


def test_the_config_phone_route_is_in_every_route_pin():
    """§4.16's `GET /api/v1/config/phone` must be in the contract pins AND both doc counts.

    A route is joined to the shipped surface by five separate lists.  A3's settle commit added it
    to all five — this test is the arithmetic that keeps a later edit from removing it from one
    (the exact failure mode `test_api_surface_parity` exists for: every behavioural test stays
    green while one list 404s the frontend's PhoneCard)."""
    from app.main import create_app
    from tests.parity.test_api_surface_parity import EXPECTED_ROUTES
    from tests.parity.test_contract_parity import DOCUMENTED_ROUTES

    path = "/api/v1/config/phone"
    assert (path, ("GET",)) in EXPECTED_ROUTES, "the published-surface pin lost the §4.16 route"
    assert ("GET", path) in DOCUMENTED_ROUTES, "the documented-surface pin lost the §4.16 route"

    live = {
        (method, route.path)
        for route in create_app().routes
        if getattr(route, "methods", None)
        for method in route.methods
        if method not in ("HEAD", "OPTIONS") and route.path.startswith("/api/v1")
    }
    assert ("GET", path) in live, "the app no longer serves the route the pins claim"

    # The two prose counts read by tests/unit/test_v28_adversarial.py.  They must equal the app.
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    completeness = (REPO / "COMPLETENESS.md").read_text(encoding="utf-8")
    for name, text, pattern in (
        ("README.md", readme, r"The `/api/v1` surface is (\d+)\s+(?:routes|method\+path operations)"),
        ("COMPLETENESS.md", completeness, r"exposes \*\*(\d+) method\+path operations\*\*"),
    ):
        match = re.search(pattern, text)
        assert match, f"{name}: the route-count sentence changed shape — update this pin"
        assert int(match.group(1)) == len(live), (
            f"{name} claims {match.group(1)} /api/v1 operations; the app serves {len(live)}"
        )


def test_the_new_error_codes_are_registered_with_a_hint_and_a_status():
    """The three A3 codes live in `app/core/errors.py` — the ONE namespace — not in a service.

    Each one's status is a deliberate value, not a fall-through: E_EGRESS_BLOCKED is a caller
    refusal (403, the E_POLICY_DENIED class); the replay refusal and E_INTERNAL are 500 BY
    DOCUMENTED DESIGN (an unlisted code is a 500, and neither is fixable by changing the request).
    """
    from app.api import deps
    from app.core import errors

    expected = {
        "E_EGRESS_BLOCKED": 403,
        "E_ACTION_REPLAY_UNAVAILABLE": 500,
        "E_INTERNAL": 500,
    }
    for code, status in expected.items():
        assert getattr(errors, code, None) == code, f"{code} is not a constant in app/core/errors.py"
        assert code in errors.ERROR_HINTS, f"{code} has no ERROR_HINTS entry (a refusal with no fix)"
        assert errors.http_status_for(code) == status, f"{code} answers the wrong HTTP status"
    assert deps.STATUS_BY_CODE is errors.HTTP_STATUS_BY_CODE, (
        "the status table was forked again — the R29 defect class (two tables, two wires)"
    )


def test_every_error_constant_has_a_hint_and_a_deliberate_status():
    """The registry-wide invariant a new code must satisfy (this is the pin A3 did not add).

    Every `E_*` string in `errors.py` must carry a `how_to_fix` hint, and must either carry a row
    in `HTTP_STATUS_BY_CODE` or be named in the explicit UNMAPPED allowlist below.  An unlisted
    code silently becomes a 500 — which is correct only when that is the DECISION, so the
    allowlist is the record of the three decisions that exist.
    """
    from app.core import errors

    unmapped_by_design = {
        "E_INTERNAL",                  # F-01: a bug we did not name -> 500 is the answer
        "E_ACTION_REPLAY_UNAVAILABLE",  # §4.13: a corrupt journal is not the caller's request
        # W8-LD-01: E_DEEPSEEK_KEY now maps to 503 (operator-fixable configuration).
    }
    codes = {
        value
        for name, value in vars(errors).items()
        if name.startswith("E_") and isinstance(value, str) and value.startswith("E_")
    }
    assert len(codes) >= 20, f"the E_* scan found only {len(codes)} codes — the scan broke"

    missing_hints = sorted(code for code in codes if code not in errors.ERROR_HINTS)
    assert not missing_hints, (
        f"code(s) with no how_to_fix: {missing_hints}. A refusal that cannot say how to fix it "
        "is the shape ERROR_HINTS exists to prevent."
    )
    missing_rows = sorted(
        code for code in codes
        if code not in errors.HTTP_STATUS_BY_CODE and code not in unmapped_by_design
    )
    assert not missing_rows, (
        f"code(s) with no HTTP status and no allowlist entry: {missing_rows}. Add the 4xx/5xx row "
        "the code means, or add it to UNMAPPED_BY_DESIGN with the reason a 500 is the decision."
    )


def test_the_status_census_carries_the_egress_row():
    """The R29 census is the second copy of the table; A3 added the 403 there too.

    Read from the shipped test module rather than re-typed, so this fails when EITHER side moves.
    """
    from app.core import errors
    from tests.unit.api.test_r29_status_parity import EXPECTED_STATUS_BY_CODE

    assert EXPECTED_STATUS_BY_CODE == errors.HTTP_STATUS_BY_CODE, (
        "the R29 status census and the canonical table disagree — a code added to one only"
    )
    assert EXPECTED_STATUS_BY_CODE["E_EGRESS_BLOCKED"] == 403


def test_the_new_registry_symbols_are_exported():
    """A new public symbol that is not in `__all__` is a dead surface (the tree's own pin rule).

    `trust_service` exported the two §4.15 symbols A3 added; `deepseek_runtime` exports the replay
    code it raises; `tool_registry` exports the F-33 meter and its bounds.  None of these is
    reachable through `from module import *` unless it is here.
    """
    from app.agents import deepseek_runtime, tool_registry
    from app.services import trust_service

    expectations = {
        trust_service: {"resolve_egress", "pinned_http_get", "E_EGRESS_BLOCKED"},
        deepseek_runtime: {"E_ACTION_REPLAY_UNAVAILABLE", "call_key", "replay_index"},
        tool_registry: {
            "begin_run_tool_budget", "end_run_tool_budget", "RunToolBudget",
            "METERED_TOOLS", "METERED_CALLS_PER_RUN", "TOOL_RESULT_CEILING",
            "WEB_FETCH_SOCKET_MAX_BYTES",
        },
    }
    for module, names in expectations.items():
        exported = set(module.__all__)
        missing = sorted(names - exported)
        assert not missing, f"{module.__name__}.__all__ is missing {missing}"


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 2. CLAIMS THAT MUST STAY TRUE — asserted at the seam the defect lived in
# ═══════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "host, why",
    [
        ("100.100.100.200", "RFC 6598 shared space — Alibaba's instance metadata (A2A1-01)"),
        ("169.254.169.254", "link-local — every cloud's metadata service"),
        ("127.0.0.1", "loopback"),
        ("::1", "IPv6 loopback"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast (is_global is True for it — the second conjunct is load-bearing)"),
    ],
)
def test_the_egress_fence_refuses_the_addresses_an_ssrf_steers_at(host, why):
    """F-27/A2A1-01: every one of these is refused BY NAME, before a socket exists.

    The old `tool_registry._is_public_ip` blacklist approved 100.100.100.200 because every flag it
    tested is False for RFC 6598.  The fence now asks the positive question (`is_global`) and names
    the range in the message, so an operator can tell policy from a resolver failure.
    """
    from app.core.errors import PolsiaError
    from app.services import trust_service

    with pytest.raises(PolsiaError) as refused:
        trust_service.resolve_egress(host, 443)
    assert refused.value.code == trust_service.E_EGRESS_BLOCKED, why
    assert refused.value.how_to_fix, f"{host}: the refusal carries no fix"


def test_a_do_h_answer_is_what_gets_refused_never_the_name(monkeypatch):
    """F-27's core: a NAME is resolved once and the ADDRESS is vetted; the name is never dialled.

    A public name whose DoH answer is private must be refused with the ADDRESS in the message
    (S1's probe measured the pre-fix tree dialling the name and never receiving the answer)."""
    from app.core.errors import PolsiaError
    from app.services import trust_service

    monkeypatch.setattr(trust_service, "_doh", lambda host, rrtype: {"status": 0, "answers": ["10.0.0.5"]})
    with pytest.raises(PolsiaError) as refused:
        trust_service.resolve_egress("example.com", 443)
    assert refused.value.code == trust_service.E_EGRESS_BLOCKED
    assert "10.0.0.5" in refused.value.message, "the refusal does not name the answer it vetted"

    monkeypatch.setattr(trust_service, "_doh", lambda host, rrtype: {"status": 3})
    with pytest.raises(PolsiaError) as nxdomain:
        trust_service.resolve_egress("nxdomain.test", 443)
    assert nxdomain.value.code == trust_service.E_EGRESS_BLOCKED
    assert "NXDOMAIN" in nxdomain.value.message or "does not resolve" in nxdomain.value.message


def test_the_web_fetch_path_has_one_connect_path():
    """F-28: `web_fetch` must dial through §4.15 (`pinned_http_get`), not its own httpx client.

    Two connect paths is how the blacklist survived beside the allowlist.  This is a SOURCE pin on
    purpose: the behaviour is already pinned with a loopback socket elsewhere, and what must never
    come back is a second connect path in the module.  (The module's other httpx client,
    `_search_client`, is the SEARCH tool's transport and is not on this path.)
    """
    from app.agents import tool_registry

    fetch_src = inspect.getsource(tool_registry._run_web_fetch)
    assert not re.search(r"httpx\.(Async)?Client", fetch_src), (
        "web_fetch grew its own httpx client — the second connect path F-28 removed"
    )
    assert "_pinned_get(" in fetch_src, "web_fetch no longer delegates its hop to the pinned helper"

    pinned_src = inspect.getsource(tool_registry._pinned_get)
    assert "pinned_http_get(" in pinned_src, "_pinned_get does not call trust_service.pinned_http_get"

    # F-32: the result tells the model the content is DATA, not instructions.
    assert '"untrusted": True' in fetch_src, "the web_fetch payload lost its untrusted flag"


# LANDED wave B1: A2A1-02 / S1-R2 — the redirect rule is the ORIGIN (scheme, host, effective port),
# not the hostname. `_web_origin` computes the triple and `_run_web_fetch` refuses a hop whose
# origin differs, so a same-host 302 cannot change the port or downgrade https->http. This test was
# that class's xfail; it is green now, and it pins the comparison by behaviour (a hostname-only
# rule would call all three of these origins equal) plus the line that consumes it.
def test_a_same_host_redirect_may_not_change_the_origin():
    from urllib.parse import urlsplit

    from app.agents import tool_registry

    same = tool_registry._web_origin(urlsplit("https://example.com/x"))
    explicit_default = tool_registry._web_origin(urlsplit("https://example.com:443/x"))
    downgraded = tool_registry._web_origin(urlsplit("http://example.com/x"))
    port_changed = tool_registry._web_origin(urlsplit("https://example.com:6379/x"))

    assert same == explicit_default == ("https", "example.com", 443), (
        "the origin wrongly distinguishes an implicit default port from the same port spelled out"
    )
    assert downgraded == ("http", "example.com", 80)
    assert port_changed == ("https", "example.com", 6379)
    assert same != downgraded, "https->http on the same host is the SAME origin: the downgrade is open"
    assert same != port_changed, "a same-host port change is the SAME origin: the port leg is open"

    fetch_src = inspect.getsource(tool_registry._run_web_fetch)
    assert "_web_origin(next_parts)" in fetch_src, (
        "web_fetch no longer computes the hop's origin — the hostname-only rule is back"
    )
    assert "next_origin != original_origin" in fetch_src, (
        "web_fetch compares something other than the full origin: a same-host 302 may change the "
        "port or scheme"
    )


def test_the_replay_refusal_is_one_code_object_and_deliberately_unmapped():
    """§4.13: the runtime raises E_ACTION_REPLAY_UNAVAILABLE; it must be the registry's own object.

    S3 defined the string in `deepseek_runtime` because `errors.py` was another seat's file; the
    settle move kept the runtime's name as an IMPORT, so `deepseek_runtime.E_ACTION_REPLAY_UNAVAILABLE`
    and `errors.E_ACTION_REPLAY_UNAVAILABLE` are the same object — two equal-but-distinct strings
    would let a caller branch on one and miss the other."""
    from app.agents import deepseek_runtime
    from app.core import errors

    assert deepseek_runtime.E_ACTION_REPLAY_UNAVAILABLE is errors.E_ACTION_REPLAY_UNAVAILABLE
    assert "E_ACTION_REPLAY_UNAVAILABLE" not in errors.HTTP_STATUS_BY_CODE, (
        "the replay refusal gained a status row: it is deliberately a 500 (a corrupt journal is "
        "not fixable by changing the request)"
    )


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 3. THE KNOWN-UNFIXED CLASSES — xfail with the engine reason + the settling command
# ═══════════════════════════════════════════════════════════════════════════════════════════════


# LANDED wave B1 (seat C6): F-19 / S6-R3 — `pricing.rates_for` resolves a SERVED alias (a dated
# release, a legacy alias, a different case) to its one pinned `MODEL_RATES` entry, so a run priced
# at the provider no longer records a NULL cost.  This test was that carry's xfail; it is green now.
# The alias table's own pins live in `tests/unit/test_b1_c_integrator.py`.
def test_a_served_model_alias_is_priced_not_unmeasured():
    from app.agents.pricing import estimate_cost_usd

    priced = estimate_cost_usd(
        "deepseek-flash-2026-10", cache_hit_tokens=0, cache_miss_tokens=1000, output_tokens=100
    )
    assert priced is not None, "a served alias is priced None: the spend gate reads it as unmeasured"


# LANDED wave B1 (seat C6): D4-R1 / A2A7-01's schema half — both money fields default to `None`,
# so the dump the worker persists says "the model reported nothing" instead of a measured `$0.00`.
# The writer half is pinned in `test_a3_spend.py`; the end-to-end dump -> `record_ads_run` pin
# (including the repeat beat that used to erase a measured $250.00) lives in
# `tests/unit/test_b1_c_integrator.py`.  The column `DEFAULT '0'` carry (S6-R6, a migration) is
# unchanged and still owed — `create_campaign` restores the NULL explicitly in-band.
def test_an_unreported_campaign_dump_carries_no_measured_zero():
    from app.schemas.agent_outputs import AdCampaignRow

    dump = AdCampaignRow(platform="google_ads", name="never reported spend").model_dump()
    assert dump["total_spent_usd"] is None, (
        "the worker's schema dump fabricates a measured 0.00 for an unreported campaign"
    )
    assert dump["daily_budget_usd"] is None


# LANDED wave B1: S2-R1/R3 (F-33, A7-04) — the worker publishes the run's measured per-channel
# counts on `RUN_CHANNEL_SENDS` before the run and clears them in `finally`, and the registry's tool
# path reads that snapshot into `decide()`. This test was the class's xfail; it is green now and
# pins the producer, the reader and the value round-trip (a context that publishes nothing still
# answers "unknown", which is the fail-closed direction).
def test_a_run_publishes_the_channel_send_snapshot():
    import inspect as _inspect

    from app.agents import policy_firewall
    from app.agents.policy_firewall import reset_run_channel_sends, set_run_channel_sends

    snapshot = {"github": {"today": 3, "hour": 1}}
    set_run_channel_sends(snapshot)
    try:
        assert policy_firewall.current_run_channel_sends() == snapshot, (
            "the published snapshot does not read back — decide()'s cap leg would see None"
        )
    finally:
        reset_run_channel_sends()
    assert policy_firewall.current_run_channel_sends() is None, (
        "the snapshot outlived its run: a later run would inherit another run's counts"
    )

    # The producer is the worker's run scope (read as source: the task is a Celery object, not a
    # plain function). It publishes before the agent runs and clears in the SAME `finally` that
    # stops the heartbeat, so one run's counts never reach the next run.
    worker_src = (BACKEND / "celery_app" / "tasks" / "agent_tasks.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert 'set_run_channel_sends(context.get("channel_sends"))' in worker_src, (
        "the worker no longer publishes the run's channel-send snapshot — every configured cap "
        "leg reads `cap_unknown` again (A7-04's write gate refuses in production)"
    )
    assert "reset_run_channel_sends()" in worker_src, (
        "the worker publishes the snapshot but never clears it: the counts leak into the next run "
        "in the same worker process"
    )

    from app.agents import tool_registry

    execute_src = _inspect.getsource(tool_registry._execute)
    assert "current_run_channel_sends()" in execute_src, (
        "the tool path no longer reads the published snapshot: the producer above would be dead "
        "code again"
    )


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 4. THE ORDER-DEPENDENCE CLASS — a rule over every test module, not one repaired instance
# ═══════════════════════════════════════════════════════════════════════════════════════════════

_ASSIGN = re.compile(r"dependency_overrides\[get_db\]\s*=")
_RESTORE_MARKERS = (
    "dependency_overrides.clear()",
    "saved_overrides",
    "monkeypatch.setitem(app.dependency_overrides",
    "pop(get_db",
    "app.dependency_overrides[get_db] = previous",
)
_OWN_APP = re.compile(r"^\s*(app|application)\s*=\s*FastAPI\(", re.MULTILINE)


def test_no_test_module_installs_a_db_override_without_a_restore():
    """The class A3 repaired once by hand (`test_a3_state.py`'s `monkeypatch.setitem`).

    A bare `app.dependency_overrides[get_db] = …` on the SHARED `app.main.app` outlives the test:
    every later request in the process reads that test's session (or a closed tmp file DB).  A3
    measured it as `test_daily_cycle_window` failing `E_NOT_FOUND … company configuration has not
    been seeded yet` in the full gate while passing alone.

    Rule: a file that installs the override on the shared app must also restore it — a `clear()`,
    a saved-copy round trip, `monkeypatch.setitem`, or an explicit `previous` write-back.  A file
    that builds its OWN `FastAPI()` cannot leak and is exempt.
    """
    offenders: list[str] = []
    for path in sorted((BACKEND / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _ASSIGN.search(text) or _OWN_APP.search(text):
            continue
        if not any(marker in text for marker in _RESTORE_MARKERS):
            line = text[: _ASSIGN.search(text).start()].count("\n") + 1
            offenders.append(f"{path.relative_to(BACKEND)}:{line}")
    assert not offenders, (
        "test module(s) install `app.dependency_overrides[get_db]` on the shared app with no "
        f"restore path: {offenders}. Use `monkeypatch.setitem(app.dependency_overrides, get_db, …)` "
        "(or save/restore around the body); a bare assignment poisons every later test in the process."
    )
