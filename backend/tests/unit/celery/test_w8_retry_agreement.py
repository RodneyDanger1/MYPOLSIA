"""WAVE 8 / logic-debugger — FAILURE MATRIX #3, GROUP B: retry/timeout AGREEMENT, measured.

The system has THREE retry/timeout ladders and ONE kill ceiling, declared in three different
places:

  1. the IN-RUN ladder (`claude_cli.run_with_retries`, shared by the Claude-CLI runtime and the
     DeepSeek runtime through `base_agent`) — attempts = 1 + `agent_max_retries`, each a provider
     call bounded by `claude_timeout_seconds` AND by what is left of the run's OWN wall clock
     (`claude_cli.run_wall_clock_budget`, derived from the worker's soft limit — W8-LD-02), plus
     jittered exponential backoff;
  2. the TASK ladder (Celery `max_retries` + `agent_tasks.run_agent_task`'s own retry decision +
     `task_service.requeue_stale_claims`'s terminal rule against `task_max_attempts`);
  3. the BEAT/SWEEP ladders (claim-lease sweep every 300 s vs a 900 s lease; stale-run close;
     stranded-pending reconcile; paused-release rearm) — each entry also declares `expires`.

Every number in this file is PRODUCED BY RUNNING the shipped ladder (a counting dispatch, a
capturing fake sleep, the imported beat schedule), never copied from a comment.  "Agreement" means:
a bound one component relies on is inside the bound another component enforces.

Rows marked xfail(strict=True) are OPEN defects with the arithmetic measured and filed; when an
owner closes one the row XPASSes and fails, which is the signal to remove the marker.
"""
from __future__ import annotations

import random

import pytest

from app.agents import claude_cli, deepseek_runtime
from app.config import settings
from app.core.errors import E_CLI_EXIT, E_CLI_TIMEOUT, MeridianError
from celery_app.tasks.agent_tasks import (
    RETRYABLE_CODES,
    is_retryable,
    retry_countdown,
    run_agent_task,
)


#: How many attempts the in-run ladder is ALLOWED to make at the shipped settings.  Derived from
#: the measured attempt count in `test_b01`; the other rows use it as the measured fact, not a copy
#: of `settings.agent_max_retries`.
def _in_run_budget() -> int:
    calls = {"n": 0}

    def _hung() -> object:
        calls["n"] += 1
        raise MeridianError(E_CLI_TIMEOUT, "planted hang")

    with pytest.raises(MeridianError):
        claude_cli.run_with_retries(_hung, sleep=lambda _s: None)
    return calls["n"]


# ── 1. the in-run ladder, measured ─────────────────────────────────────────────────────────
def test_b01_the_in_run_ladder_makes_1_plus_agent_max_retries_attempts():
    calls = {"n": 0}

    def _hung() -> object:
        calls["n"] += 1
        raise MeridianError(E_CLI_TIMEOUT, "planted hang")

    with pytest.raises(MeridianError) as caught:
        claude_cli.run_with_retries(_hung, sleep=lambda _s: None)

    assert caught.value.code == E_CLI_TIMEOUT, "the last attempt's refusal is re-raised by name"
    assert calls["n"] == 1 + int(settings.agent_max_retries), (
        f"the ladder made {calls['n']} attempts; the declared budget is "
        f"1 + agent_max_retries={1 + int(settings.agent_max_retries)}"
    )


def test_b02_a_hard_failure_is_not_retried_at_all():
    """Agreement inside one run: only a raised timeout is retried; a hard exit is final."""
    calls = {"n": 0}

    def _hard() -> object:
        calls["n"] += 1
        raise MeridianError(E_CLI_EXIT, "planted hard exit")

    with pytest.raises(MeridianError) as caught:
        claude_cli.run_with_retries(_hard, sleep=lambda _s: None)
    assert caught.value.code == E_CLI_EXIT
    assert calls["n"] == 1, "a non-timeout refusal paid for exactly one provider call"


def test_b03_a_classified_transient_envelope_is_retried_to_the_bound():
    calls = {"n": 0}

    def _transient() -> str:
        calls["n"] += 1
        return "retry-me"

    _, used = claude_cli.run_with_retries(
        _transient, classify=lambda _r: True, sleep=lambda _s: None
    )
    assert calls["n"] == used == _in_run_budget()


def test_b04_the_backoff_is_seeded_monotone_jitter_inside_the_documented_band():
    """`base * 2**attempt * U(0.5, 1.0)`, capped at 30 s — measured through the ladder's sleeps."""
    slept: list[float] = []
    calls = {"n": 0}

    def _hung() -> object:
        calls["n"] += 1
        raise MeridianError(E_CLI_TIMEOUT, "planted hang")

    rng = random.Random(20261029)  # seeded so the assertion is deterministic, not flaky
    with pytest.raises(MeridianError):
        claude_cli.run_with_retries(_hung, sleep=lambda s: slept.append(s), rng=rng)

    expected_rng = random.Random(20261029)
    expected = [
        claude_cli.backoff_delay(attempt, base_s=settings.agent_retry_backoff_seconds, rng=expected_rng)
        for attempt in range(len(slept))
    ]
    assert slept == pytest.approx(expected), (slept, expected)
    for attempt, value in enumerate(slept):
        band = settings.agent_retry_backoff_seconds * (2**attempt)
        assert 0.5 * band <= value <= band or value == 30.0, (
            f"attempt {attempt} slept {value}s outside the declared band"
        )
    assert claude_cli.backoff_delay(50, base_s=settings.agent_retry_backoff_seconds) == 30.0, (
        "the backoff must be capped so a deep ladder cannot sleep past the task's own deadline"
    )


def test_b05_the_deepseek_runtime_reads_the_SAME_ladder_and_the_same_call_bound():
    """One ladder, two runtimes: the constraint is `claude_cli.run_with_retries` + a call timeout."""
    inv = claude_cli.Invocation(argv=["x"], timeout_s=settings.claude_timeout_seconds)
    deepseek_bound = deepseek_runtime._call_timeout_seconds(inv)
    provider_bound = deepseek_runtime._timeout_seconds()
    assert provider_bound == settings.claude_timeout_seconds == deepseek_bound, (
        "both runtimes must bound ONE provider call by the same number, or the in-run worst case "
        f"differs by runtime: claude={settings.claude_timeout_seconds} "
        f"deepseek_provider={provider_bound} deepseek_call={deepseek_bound}"
    )


# ── 2. the in-run ladder vs the worker's kill ceiling ─────────────────────────────────────
def test_b06_the_in_run_worst_case_is_derived_from_the_runs_OWN_budget():
    """The worst case is no longer `attempts x per-call`: it is the run's own budget (W8-LD-02).

    ARITHMETIC CHANGED IN WAVE 9 — old and new, both from the shipped numbers:

      OLD (wave 8, measured open defect): attempts x claude_timeout_seconds + backoff
          = 3 x 300 s + 4.5 s = **904.5 s**   ->  904.5 > 840 s soft limit (row B07 was xfail)
      NEW (this fix): the run carries ONE deadline, `claude_cli.run_wall_clock_budget()`
          budget = soft - settle_reserve - backoff = 840 - 60 - 4.5 = **775.5 s**
          worst  = budget + backoff                = 775.5 + 4.5   = **780.0 s**
        and every provider call is clamped to what is LEFT of the budget (B07b measures the
        clamp itself, so this is not a comment).

    Neither the per-call ceiling nor the attempt count changed: a single call may still use the
    full `claude_timeout_seconds`, and the ladder still makes 1 + agent_max_retries attempts.
    What changed is that the attempts SHARE one wall clock derived from the worker's own kill
    ceiling, so the worst case is a consequence of the configuration instead of a second opinion.
    """
    attempts = _in_run_budget()
    backoff_total = claude_cli.ladder_backoff_total_seconds(attempts)
    budget = claude_cli.run_wall_clock_budget()
    reserve = claude_cli.settle_reserve_seconds()

    assert budget == pytest.approx(
        settings.celery_soft_time_limit - reserve - backoff_total
    ), "the run budget must be DERIVED from the soft limit, not a literal"
    assert budget == pytest.approx(775.5), (
        f"the derived run budget changed ({budget}); re-measure the agreement rows"
    )
    worst = budget + backoff_total
    assert worst == pytest.approx(780.0), (
        f"the in-run worst case changed; re-measure it (old 904.5 -> new {worst})"
    )

    # the budget reaches the ONE invocation builder every agent uses ...
    inv = claude_cli.build_invocation(prompt="budget probe", timeout_s=settings.claude_timeout_seconds)
    assert inv.run_budget_seconds == pytest.approx(budget)
    assert inv.remaining_seconds() is not None and inv.remaining_seconds() <= budget
    # ... and the per-call ceiling is untouched (capability is not what was traded away)
    assert inv.timeout_s == settings.claude_timeout_seconds


def test_b07_the_in_run_ladder_fits_inside_the_worker_that_runs_it():
    """W8-LD-02, closed: the run's worst case fits inside the worker, settle reserve included."""
    attempts = _in_run_budget()
    backoff_total = claude_cli.ladder_backoff_total_seconds(attempts)
    budget = claude_cli.run_wall_clock_budget()
    reserve = claude_cli.settle_reserve_seconds()
    worst = budget + backoff_total

    assert worst <= settings.celery_soft_time_limit + 1e-6, (
        f"the in-run ladder needs {worst:.1f}s but the worker kills it at "
        f"{settings.celery_soft_time_limit}s"
    )
    assert worst + reserve <= settings.celery_soft_time_limit + 1e-6, (
        f"the run must also be able to SETTLE after its last call: worst {worst:.1f}s + settle "
        f"reserve {reserve:.1f}s > soft limit {settings.celery_soft_time_limit}s — the task row "
        "would wait for the lease sweep and the run row for the stale-run reaper"
    )


def test_b07b_the_run_budget_is_ENFORCED_by_both_runtimes_not_just_declared():
    """The bound has to bite: a spent budget refuses BY NAME and never reaches the provider.

    Legs (each measured, now that the arithmetic in B06/B07 is only a declaration without them):
      a. `claude_cli.invoke` with a spent budget raises `E_CLI_TIMEOUT` and does NOT spawn;
      b. `claude_cli.invoke` with 5 s left of the budget hands the SPAWN exactly that bound
         (not the 300 s ceiling), so a hung CLI cannot outlive the run;
      c. `deepseek_runtime.invoke` with a spent budget raises the same code and makes ZERO
         provider calls (the tool loop's multiplier is therefore bounded, not just the first call).
    """
    import time

    import httpx

    from app.core.errors import E_CLI_TIMEOUT

    # (a) spent budget -> named refusal, no spawn
    spent = claude_cli.Invocation(
        argv=["claude", "-p", "x"],
        timeout_s=settings.claude_timeout_seconds,
        run_budget_seconds=1.0,
        deadline_monotonic=time.monotonic() - 1.0,
    )
    spawned: list[tuple] = []
    with pytest.raises(MeridianError) as caught:
        claude_cli.invoke(spent, spawn=lambda *args: spawned.append(args) or ("{}", 0))
    assert caught.value.code == E_CLI_TIMEOUT
    assert caught.value.context["remaining_seconds"] < 0
    assert spawned == [], "a spent budget must never spawn a CLI (that is the 904.5 s defect)"

    # (b) 5 s left -> the spawn receives at most 5 s, never the 300 s ceiling
    live = claude_cli.Invocation(
        argv=["claude", "-p", "x"],
        timeout_s=settings.claude_timeout_seconds,
        run_budget_seconds=5.0,
        deadline_monotonic=time.monotonic() + 5.0,
    )
    seen: dict[str, float] = {}

    def _spawn(_argv, _stdin, _env, timeout_s):
        seen["timeout"] = timeout_s
        return '{"result": "ok"}', 0

    claude_cli.invoke(live, spawn=_spawn)
    assert 1 <= seen["timeout"] <= 5, (
        f"the enforced call bound was {seen['timeout']}s with 5 s of run budget left"
    )

    # (c) the DeepSeek runtime refuses before the provider is reachable
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    client = httpx.Client(transport=transport)
    try:
        with pytest.raises(MeridianError) as caught_http:
            deepseek_runtime.invoke(
                claude_cli.Invocation(
                    argv=["claude", "-p", "x"],
                    timeout_s=settings.claude_timeout_seconds,
                    run_budget_seconds=1.0,
                    deadline_monotonic=time.monotonic() - 1.0,
                ),
                client=client,
                api_key="probe-key",
            )
    finally:
        client.close()
    assert caught_http.value.code == E_CLI_TIMEOUT
    assert calls == [], "a spent budget must never reach the provider"


# ── 3. the task ladder, measured ──────────────────────────────────────────────────────────
def test_b08_the_task_retry_countdown_doubles_and_is_capped():
    values = [retry_countdown(attempts) for attempts in range(1, 8)]
    assert values[0] == float(settings.task_retry_backoff_seconds)
    for previous, current in zip(values, values[1:], strict=False):
        assert current >= previous, f"the retry backoff shrank: {values}"
    assert values[:3] == [
        float(settings.task_retry_backoff_seconds) * factor for factor in (1, 2, 4)
    ], values
    assert max(values) == 600.0, "the cap is the 10-minute ceiling the docstring promises"
    assert retry_countdown(0) == float(settings.task_retry_backoff_seconds), (
        "attempts=0 (a caller's own first failure) waits one base delay, never a negative one"
    )


def test_b09_the_celery_retry_budget_and_the_stale_claim_terminal_rule_agree():
    """Two knobs name the same bound today; the test MEASURES both, so a change is visible."""
    executions_by_celery = 1 + int(run_agent_task.max_retries or 0)
    executions_by_retry_decision = 1 + int(settings.agent_max_retries)
    executions_by_reaper_rule = int(settings.task_max_attempts)
    assert run_agent_task.max_retries == settings.agent_max_retries, (
        "the decorator's own max_retries and the retry decision must be the same knob"
    )
    assert executions_by_celery == executions_by_retry_decision == executions_by_reaper_rule, (
        "DISAGREEMENT: the retry ladder allows "
        f"{executions_by_retry_decision} executions, Celery allows {executions_by_celery}, and the "
        f"lease reaper ends the row after {executions_by_reaper_rule} — a row can be failed by the "
        "reaper while its ladder still intends to retry (or spin past the reaper's bound)"
    )


def test_b10_the_task_classifier_and_the_in_run_classifier_agree_on_a_terminal_provider_refusal():
    """A rejected API key / empty provider account is operator-terminal in BOTH ladders."""
    # 1. the in-run classifier refuses to retry a 401/402 envelope ...
    assert claude_cli.is_retryable_failure(
        E_CLI_EXIT, subtype="authentication_error", api_status=401, exit_code=1
    ) is False
    assert claude_cli.is_retryable_failure(
        E_CLI_EXIT, subtype="invalid_api_key", api_status=None, exit_code=1
    ) is False
    # 2. ... and the task ladder refuses to requeue the refusal the runtime then raises, when the
    #    refusal DECLARES its verdict (the wave-7 contract).
    declared = MeridianError(
        E_CLI_EXIT,
        "the provider rejected the key",
        context={"retryable": False, "reason": "provider_auth_rejected"},
    )
    assert is_retryable(declared) is False
    # 3. a transient envelope stays retryable on both ladders.
    assert claude_cli.is_retryable_failure(
        E_CLI_EXIT, subtype="rate_limit", api_status=429, exit_code=1
    ) is True
    assert E_CLI_EXIT in RETRYABLE_CODES


def test_b11_the_task_classifier_respects_an_explicit_verdict_over_the_code_table():
    retry_me = MeridianError(E_CLI_TIMEOUT, "transient")
    stop = MeridianError(E_CLI_TIMEOUT, "terminal", context={"retryable": False})
    yes = MeridianError("E_SOMETHING_TRANSIENT", "x", context={"retryable": True})
    assert is_retryable(retry_me) is True
    assert is_retryable(stop) is False
    assert is_retryable(yes) is True, "an explicit True must be honoured, not just False"
    junk = MeridianError(E_CLI_TIMEOUT, "x", context={"retryable": "no"})
    assert is_retryable(junk) is True, "a malformed verdict falls back to the code table"


# ── 4. the beat/sweep ladders vs the lease they recover ───────────────────────────────────
def test_b12_the_lease_sweep_interval_is_strictly_shorter_than_the_lease():
    from celery_app.beat_schedule import beat_schedule

    entry = beat_schedule["task-lease-sweep"]
    interval = float(entry["schedule"])
    assert interval < settings.task_claim_lease_seconds, (
        f"the sweep ({interval}s) must run inside the lease ({settings.task_claim_lease_seconds}s) "
        "or a dead worker's row waits two leases"
    )
    assert entry["options"]["expires"] < interval, (
        "a tick that outlives its own interval would stack deliveries"
    )
    worst_recovery = settings.task_claim_lease_seconds + interval
    assert worst_recovery == 1200.0, (
        "the measured recovery bound (lease + one tick) changed; the reaper-window rows in the "
        f"matrix quote it: {worst_recovery}"
    )


@pytest.mark.parametrize(
    "name",
    [
        "task-lease-sweep",
        "reconcile-stranded-pending",
        "stale-agent-runs-sweep",
        "paused-release-rearm",
        "phone-activity-tick",
    ],
)
def test_b13_every_recovery_tick_declares_an_expiry_shorter_than_its_interval(name: str):
    """Measured from the imported schedule: a recovery tick cannot outlive its own period."""
    from celery_app.beat_schedule import beat_schedule

    entry = beat_schedule[name]
    schedule = entry["schedule"]
    expires = entry["options"]["expires"]
    if isinstance(schedule, int | float):
        interval = float(schedule)
    else:
        assert expires <= 900, (
            f"{name}: a crontab tick's expiry must be bounded so a backlog cannot replay it "
            f"({expires}s)"
        )
        return
    assert expires < interval, (name, expires, interval)


def test_b14_the_sweeps_that_exist_are_the_ones_with_a_registered_task():
    from celery import current_app

    from celery_app.beat_schedule import beat_schedule

    for name, entry in beat_schedule.items():
        task_name = entry["task"]
        assert task_name in current_app.tasks, (
            f"beat entry {name!r} names {task_name!r}, which no worker registers"
        )


# ── 5. the broker's redelivery clock vs the two clocks it must not race ───────────────────
def test_b15_the_broker_visibility_timeout_covers_the_lease_and_the_hard_limit():
    from celery_app.celery_config import visibility_timeout

    assert visibility_timeout >= settings.task_claim_lease_seconds
    assert visibility_timeout >= settings.celery_task_time_limit + 600, (
        "a redelivery must not land while the task could still be settling (the persist-and-ack "
        "phase runs unacked)"
    )
    assert visibility_timeout == max(
        2 * settings.task_claim_lease_seconds, settings.celery_task_time_limit + 600
    )
