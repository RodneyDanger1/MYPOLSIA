"""RD4 items 1 + 2 — kill-switch settings fallback, and `safety_preflight` wired into dispatch.

Both ride the REAL seam: `crew_factory.dispatch_preflight` — the gate that already runs before
every `run_agent_for_task` — with the real `policy_firewall.kill_grade_from_env` and the real
`claude_cli.safety_preflight`.  No stand-ins are used for the chain under test.

Item 1 (audit L2): `kill_grade_from_env` read `settings.kill_switch`, a field that did not exist,
so the settings half of the kill switch was permanently `""`.  The test below pins that path by
monkeypatching the settings object — which could not even be written before this wave if pydantic
refuses the undeclared field (both worlds are handled, see `_pin_settings_kill_switch`).

Item 2 (audit L2-F08): `safety_preflight` had zero callers, so a missing CLI binary or a
logged-out CLI surfaced only at spawn time.  The tests drive `dispatch_preflight`/`run_agent_for_task`
with simulated host states and assert the NAMED refusal — and, just as important, that the CLI legs
do NOT refuse a runtime that would never spawn a CLI (mock mode, non-CLI backend).

RED legs (measured this session; lane receipt build/RD4-unwired/RECEIPT.md):
  * item 1 — neutralising the settings fallback (always "") fails
    `test_settings_kill_switch_freezes_when_env_is_unset`;
  * item 2 — dropping the `safety_preflight(...)` call from `dispatch_preflight` fails
    `test_a_missing_cli_binary_refuses_dispatch_before_any_spawn` and
    `test_a_logged_out_cli_refuses_dispatch_before_any_spawn`.
"""
from __future__ import annotations

import contextlib

import pytest

import app.config as app_config
from app.agents import crew_factory
from app.core.errors import E_CLI_JSON, E_CLI_MISSING, E_POLICY_DENIED, PolsiaError


def _pin_settings_kill_switch(monkeypatch, request, value: str) -> None:
    """Pin `settings.kill_switch`, in both worlds.

    With the field declared (this wave's sibling edit) a normal monkeypatch lands.  While it is
    ABSENT — the audited state — pydantic v2 raises `ValueError` on assignment to an undeclared
    field, so the value is pinned through `object.__setattr__` (the same instance `__dict__` a real
    field write lands in) and removed at tear-down.  The production read is
    `getattr(settings, "kill_switch", "")`, so this exercises the real path either way.
    """
    try:
        monkeypatch.setattr(app_config.settings, "kill_switch", value, raising=False)
        return
    except ValueError:
        pass
    target = app_config.settings
    object.__setattr__(target, "kill_switch", value)

    def _restore() -> None:
        with contextlib.suppress(AttributeError):
            object.__delattr__(target, "kill_switch")

    request.addfinalizer(_restore)


def _pin_deepseek_availability(monkeypatch, available: bool) -> None:
    """Pin the provider seam's predicate so the CLI legs are decided by THIS test, not the host.

    `crew_factory._non_cli_backend()` asks `deepseek_runtime.available()` (exactly what
    `base_agent._real_call` routes on); a developer machine with a stored DeepSeek key would
    otherwise change what these tests measure.
    """
    try:
        from app.agents import deepseek_runtime
    except ImportError:  # pragma: no cover - module owned by a parallel seat
        return
    monkeypatch.setattr(deepseek_runtime, "available", lambda: available)


def _non_mock_runtime(monkeypatch) -> None:
    """Make the runtime look like a real (non-mock) deployment with a resolvable bare CLI name."""
    monkeypatch.delenv("CLAUDE_CLI_MOCK", raising=False)
    monkeypatch.setattr(app_config.settings, "claude_cli_mock", False)
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setattr(app_config.settings, "claude_cli_path", "claude")
    monkeypatch.delenv("POLSIA_MODEL_BACKEND", raising=False)
    _pin_deepseek_availability(monkeypatch, False)
    monkeypatch.setattr(crew_factory, "_CLI_AUTH_VERDICTS", {})


class _FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ── item 1 — the settings fallback is real, and the env still wins ──────────────────────────────
def test_settings_kill_switch_freezes_when_env_is_unset(monkeypatch, request):
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)
    _pin_settings_kill_switch(monkeypatch, request, "freeze")

    problems = crew_factory.dispatch_preflight("email_outreach")
    assert problems, "a settings-only freeze must refuse dispatch (fresh field, real read path)"
    assert any("freeze" in p for p in problems)

    # …and the same settings value stops the WHOLE dispatch, not just the preflight list.
    with pytest.raises(PolsiaError) as exc_info:
        crew_factory.run_agent_for_task("email_outreach", {}, {})
    assert exc_info.value.code == E_POLICY_DENIED
    assert exc_info.value.context["kill_grade"] == "freeze"


def test_settings_kill_switch_throttle_stops_external_agents_only(monkeypatch, request):
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)
    _pin_settings_kill_switch(monkeypatch, request, "throttle")

    stopped = crew_factory.dispatch_preflight("email_outreach")          # external side effects
    assert any("throttle" in p for p in stopped)
    assert crew_factory.dispatch_preflight("business_planning") == []    # internal writes only


def test_the_env_still_wins_over_the_settings_field(monkeypatch, request):
    """Precedence, pinned: an operator's shell-level stop is the fastest one there is."""
    _pin_settings_kill_switch(monkeypatch, request, "freeze")
    monkeypatch.setenv("POLSIA_KILL_SWITCH", "run")
    assert crew_factory.dispatch_preflight("email_outreach") == []

    monkeypatch.setenv("POLSIA_KILL_SWITCH", "freeze")
    assert crew_factory.dispatch_preflight("email_outreach")


def test_the_freeze_refusal_is_reported_once(monkeypatch, request):
    """Both gates hold on a freeze, but the refusal names it once (no duplicated kill-switch line)."""
    monkeypatch.setenv("POLSIA_KILL_SWITCH", "freeze")
    problems = crew_factory.dispatch_preflight("email_outreach")
    kill_lines = [p for p in problems if "kill switch" in p.lower()]
    assert len(kill_lines) == 1, f"expected one kill-switch refusal, got {problems}"


# ── item 2 — the spawn-level safety preflight runs, and refuses BEFORE any spawn ────────────────
def test_a_missing_cli_binary_refuses_dispatch_before_any_spawn(monkeypatch):
    """CLI-missing sim: the binary is not resolvable and the CLI IS the path ⇒ named refusal."""
    _non_mock_runtime(monkeypatch)
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)
    monkeypatch.setattr(crew_factory.shutil, "which", lambda _name: None)

    def _no_spawn(*_args, **_kwargs):  # a spawn attempt here would mean the gate failed
        raise AssertionError("dispatch must refuse before anything is spawned")

    monkeypatch.setattr(crew_factory.subprocess, "run", _no_spawn)

    problems = crew_factory.dispatch_preflight("email_outreach")
    assert any(E_CLI_MISSING in p for p in problems), problems

    with pytest.raises(PolsiaError) as exc_info:
        crew_factory.run_agent_for_task("email_outreach", {}, {})
    assert exc_info.value.code == E_POLICY_DENIED
    assert E_CLI_MISSING in exc_info.value.message
    safety = exc_info.value.context["safety"]
    assert safety["cli_present"] is False
    assert safety["cli_auth"] == "not_probed"


def test_a_logged_out_cli_refuses_dispatch_before_any_spawn(monkeypatch):
    """Auth sim: the real probe's parsing runs on the CLI's own logged-out wording."""
    _non_mock_runtime(monkeypatch)
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)
    monkeypatch.setattr(crew_factory.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen: list[list[str]] = []

    def _run(argv, **_kwargs):
        seen.append(list(argv))
        return _FakeProc(stdout="You are not logged in. Run `claude login` to authenticate.",
                         returncode=1)

    monkeypatch.setattr(crew_factory.subprocess, "run", _run)

    problems = crew_factory.dispatch_preflight("email_outreach")
    assert any(E_CLI_JSON in p for p in problems), problems
    assert seen == [["claude", "auth", "status"]], "the probe must ask the CLI itself"

    with pytest.raises(PolsiaError) as exc_info:
        crew_factory.run_agent_for_task("email_outreach", {}, {})
    assert E_CLI_JSON in exc_info.value.message
    assert exc_info.value.context["safety"]["cli_auth"] == "logged_out"


def test_an_inconclusive_auth_probe_does_not_refuse_the_fleet(monkeypatch):
    """Honesty rule: a probe that cannot judge must NOT become a refusal.

    An older CLI without the subcommand (or a timeout) is recorded as `unverified`; the
    spawn-time CLI_AUTH_FAILED -> E_CLI_EXIT mapping stays the second line of defence.
    """
    _non_mock_runtime(monkeypatch)
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)
    monkeypatch.setattr(crew_factory.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _explode(*_args, **_kwargs):
        raise OSError("no such subcommand")

    monkeypatch.setattr(crew_factory.subprocess, "run", _explode)

    assert crew_factory.dispatch_preflight("email_outreach") == []
    assert crew_factory._spawn_facts()["cli_auth"] == "unverified"


def test_the_auth_probe_runs_once_per_process(monkeypatch):
    _non_mock_runtime(monkeypatch)
    monkeypatch.setattr(crew_factory.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []

    def _run(argv, **_kwargs):
        calls.append(list(argv))
        return _FakeProc(stdout="Logged in as ops@example.com", returncode=0)

    monkeypatch.setattr(crew_factory.subprocess, "run", _run)

    assert crew_factory.dispatch_preflight("email_outreach") == []
    assert crew_factory.dispatch_preflight("social_media") == []
    assert len(calls) == 1, f"the verdict must be cached, saw {len(calls)} probes"


def test_mock_mode_never_refuses_for_a_missing_cli(monkeypatch):
    """The CLI legs are NOT APPLICABLE when nothing spawns (CLAUDE_CLI_MOCK) — not a fake pass:
    the reason is recorded so the verdict is auditable."""
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)
    monkeypatch.setattr(crew_factory.shutil, "which", lambda _name: None)

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError("mock mode must not spawn a CLI process")

    monkeypatch.setattr(crew_factory.subprocess, "run", _no_spawn)

    assert crew_factory.dispatch_preflight("email_outreach") == []
    facts = crew_factory._spawn_facts()
    assert facts["backend"] == "mock"
    assert facts["cli_auth"] == "not_applicable"
    assert "MOCK" in facts["note"]


def test_a_non_cli_backend_does_not_need_the_cli_binary(monkeypatch):
    """The DeepSeek-shaped runtime: a declared non-CLI backend means a missing binary is not a
    failure of a path that would never be taken."""
    _non_mock_runtime(monkeypatch)
    monkeypatch.setenv("POLSIA_MODEL_BACKEND", "deepseek")
    monkeypatch.setattr(crew_factory.shutil, "which", lambda _name: None)

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError("a non-CLI backend must not probe/spawn the claude CLI")

    monkeypatch.setattr(crew_factory.subprocess, "run", _no_spawn)

    assert crew_factory.dispatch_preflight("email_outreach") == []
    assert crew_factory._spawn_facts()["backend"] == "deepseek"


def test_a_resolvable_deepseek_key_does_not_require_the_cli(monkeypatch):
    """The provider seam's OWN predicate decides it — the same call `base_agent._real_call` makes.

    With a DeepSeek key resolvable, agent runs go to the HTTP runtime, so a missing claude binary
    must not refuse dispatch.  This is the DeepSeek-parallel lane's case, pinned here so the
    preflight cannot regress into a fleet-wide refusal the day a key is configured.
    """
    _non_mock_runtime(monkeypatch)
    _pin_deepseek_availability(monkeypatch, True)
    monkeypatch.setattr(crew_factory.shutil, "which", lambda _name: None)

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError("the DeepSeek path must not probe/spawn the claude CLI")

    monkeypatch.setattr(crew_factory.subprocess, "run", _no_spawn)

    assert crew_factory.dispatch_preflight("email_outreach") == []
    assert crew_factory._spawn_facts()["backend"] == "deepseek"


def test_an_unknown_backend_signal_still_requires_the_cli(monkeypatch):
    """Fail closed: an unrecognised backend is the claude-cli path, so the binary check applies."""
    _non_mock_runtime(monkeypatch)
    monkeypatch.setenv("POLSIA_MODEL_BACKEND", "something-new")
    monkeypatch.setattr(crew_factory.shutil, "which", lambda _name: None)

    problems = crew_factory.dispatch_preflight("email_outreach")
    assert any(E_CLI_MISSING in p for p in problems), problems


def test_throttle_semantics_are_preserved_alongside_the_safety_gate(monkeypatch):
    """Both gates hold: throttle still stops external-action agents while the safety legs are clear."""
    monkeypatch.setenv("POLSIA_KILL_SWITCH", "throttle")
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    stopped = crew_factory.dispatch_preflight("ads_management")
    assert any("throttle" in p for p in stopped)
    assert crew_factory.dispatch_preflight("competitor_research") == []  # reads/web only
