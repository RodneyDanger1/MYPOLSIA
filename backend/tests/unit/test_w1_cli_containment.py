"""W1 / AC-02 (P0) — the Claude CLI fallback cannot reach the backend's credential stores.

THE DEFECT (BUGS.json AC-02, `backend/app/agents/claude_cli.py` / `policy_firewall.py`):
`build_invocation` set no `cwd`, so `subprocess.Popen(cwd=inv.cwd)` inherited the WORKER's
directory — the backend directory holding `.env`, `data/secrets.json` and
`data/deepseek_api_key.json` — and the file-tool allowlist granted bare `Read` / `Edit` / `Write`
with no path specifier.  On the documented no-DeepSeek-key fallback the CLI was a whole-account
credential surface, exactly the Postmark failure mode.

WHAT THESE TESTS PIN (all locally verifiable — see the UNVERIFIED note at the end):
  1. every invocation carries a NON-None cwd, it is created at spawn time, and it is neither
     inside nor a container of any local credential store;
  2. `subprocess.Popen` is called with that cwd and with a child environment built ONLY from the
     `SAFE_ENV_PASSTHROUGH` whitelist — no provider key inherited, no secret value in the env;
  3. a caller cannot smuggle a credential-bearing variable in through `env_overrides`/`extra_env`,
     and cannot re-grant a credential store with `--add-dir`;
  4. an uncreatable run workspace is the named `E_STORAGE` refusal, never a fallback to the
     inherited cwd;
  5. every file-tool ALLOW rule on the two agents that hold file tools is path-scoped
     (`Tool(./**)`), and the CLI argv cannot name a credential file.

THE UNVERIFIED LEG, stated rather than implied: the `claude` binary is NOT installed on this
host (`Get-Command claude` -> absent), so the CLI's own resolution of `Read(./**)` against its
cwd cannot be executed here.  Everything above is process-local and asserted; what remains
outside this process is the CLI's own file-access policy for an ABSOLUTE path the model types.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agents import claude_cli, policy_firewall
from app.core.errors import E_POLICY_DENIED, E_STORAGE, MeridianError

FILE_TOOLS = ("Read", "Edit", "Write")


# ── helpers ─────────────────────────────────────────────────────────────────────────────────
class _FakePopen:
    """A fake process boundary: records the EXACT kwargs `invoke()` hands the real Popen."""

    captured: dict[str, object] = {}

    def __init__(self, argv, **kwargs) -> None:  # noqa: ARG002 - argv kept by the caller's assertion
        _FakePopen.captured.clear()
        _FakePopen.captured.update(kwargs)
        _FakePopen.captured["is_dir_at_spawn"] = Path(str(kwargs.get("cwd"))).is_dir()
        self.pid = 1
        self.returncode = 0

    def communicate(self, input=None, timeout=None):  # noqa: ARG002 - interface parity
        return ('{"type":"result","subtype":"success","is_error":false,"result":"{}"}', "")

    def poll(self):
        return 0


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.captured = {}
    monkeypatch.setattr(claude_cli.subprocess, "Popen", _FakePopen)
    return _FakePopen.captured


def _secret_paths() -> tuple[Path, ...]:
    return claude_cli.secret_store_paths()


# ── 1. the built invocation's cwd ───────────────────────────────────────────────────────────
def test_the_built_invocation_carries_a_non_none_run_workspace(tmp_path: Path) -> None:
    inv = claude_cli.build_invocation(prompt="probe", workspace_root=tmp_path)

    assert inv.cwd is not None, "AC-02: cwd=None is the inherited-worker-directory defect"
    cwd = Path(inv.cwd).resolve()
    assert cwd.is_relative_to(tmp_path.resolve()), "the run directory lives under its configured root"
    assert cwd != claude_cli.backend_root()
    assert claude_cli.workspace_refusal(cwd) is None
    for secret in _secret_paths():
        assert not cwd.is_relative_to(secret.parent), f"cwd is inside the store directory holding {secret}"
        assert not secret.is_relative_to(cwd), f"cwd contains the credential store {secret}"


def test_the_default_root_is_outside_the_backend_credential_tree() -> None:
    """No operator override: the default root must not be the backend tree (no test seam)."""
    inv = claude_cli.build_invocation(prompt="probe")
    cwd = Path(str(inv.cwd)).resolve()

    assert claude_cli.workspace_refusal(cwd) is None
    assert not cwd.is_relative_to(claude_cli.backend_root()), (
        f"the default CLI cwd {cwd} is inside the backend tree that holds .env and data/*.json"
    )
    for secret in _secret_paths():
        assert str(secret) not in inv.redacted_preview(), "a credential path must never ride argv"


# ── 2. the Popen boundary: cwd + child environment ─────────────────────────────────────────
def test_popen_gets_the_workspace_cwd_and_no_provider_secret(monkeypatch, fake_popen, tmp_path: Path) -> None:
    sentinels = {
        "DEEPSEEK_API_KEY": "sk-deepseek-w1-probe-not-a-real-key",
        "ANTHROPIC_API_KEY": "sk-ant-w1-probe-not-a-real-key",
        "SENDGRID_API_KEY": "SG.w1-probe-not-a-real-key",
        "STRIPE_SECRET_KEY": "sk_live_w1_probe_not_a_real_key",
    }
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)

    inv = claude_cli.build_invocation(prompt="probe", workspace_root=tmp_path)
    claude_cli.invoke(inv)

    cwd = fake_popen.get("cwd")
    assert cwd is not None, "AC-02: the child must never inherit the worker's cwd"
    assert fake_popen.get("is_dir_at_spawn") is True, "the workspace exists before the spawn"
    assert Path(str(cwd)).resolve() == Path(str(inv.cwd)).resolve()
    assert Path(str(cwd)).resolve().is_relative_to(tmp_path.resolve())

    env = fake_popen.get("env")
    assert isinstance(env, dict)
    assert set(env) <= set(claude_cli.SAFE_ENV_PASSTHROUGH), (
        "the child environment is the whitelist plus nothing: " + repr(sorted(set(env) - set(claude_cli.SAFE_ENV_PASSTHROUGH)))
    )
    for name in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "SENDGRID_API_KEY", "STRIPE_SECRET_KEY"):
        assert name not in env, f"{name} must not reach the CLI child"
    for value in sentinels.values():
        assert value not in env.values(), "no secret VALUE may appear in the child environment"

    argv_text = " ".join(str(a) for a in inv.argv)
    for secret in _secret_paths():
        assert str(secret) not in argv_text
    assert "secrets.json" not in argv_text and "deepseek_api_key.json" not in argv_text

    # The invoke() finally removed the empty per-run directory it created (no residue).
    assert not Path(str(cwd)).exists() or any(Path(str(cwd)).iterdir())


def test_a_hand_built_invocation_still_never_spawns_with_a_none_cwd(fake_popen) -> None:
    """The pre-fix `Invocation` shape (cwd None) is still contained at the spawn boundary."""
    inv = claude_cli.Invocation(argv=["claude", "-p", "hi"], timeout_s=5)
    assert inv.cwd is None, "this is the shape the defect used — invoke() must not trust it"

    claude_cli.invoke(inv)

    cwd = fake_popen.get("cwd")
    assert cwd is not None
    assert fake_popen.get("is_dir_at_spawn") is True
    assert claude_cli.workspace_refusal(cwd) is None


def test_an_uncreatable_run_workspace_is_a_named_refusal_not_an_inherited_cwd(
    monkeypatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is required", encoding="utf-8")

    def poison(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("a CLI process was spawned without its own workspace")

    monkeypatch.setattr(claude_cli.subprocess, "Popen", poison)
    inv = claude_cli.Invocation(
        argv=["claude", "-p", "hi"], cwd=str(blocker / "runs" / "cli-w1"), timeout_s=5
    )

    with pytest.raises(MeridianError) as caught:
        claude_cli.invoke(inv)
    assert caught.value.code == E_STORAGE
    assert caught.value.how_to_fix
    assert "workspace" in caught.value.message.lower()


# ── 3. explicit overrides cannot re-introduce a credential ─────────────────────────────────
def test_the_child_environment_refuses_a_credential_shaped_override() -> None:
    with pytest.raises(MeridianError) as caught:
        claude_cli.child_environment(extra_env={"SENDGRID_API_KEY": "SG.w1-probe"})
    assert caught.value.code == E_POLICY_DENIED
    assert "SENDGRID_API_KEY" in caught.value.message
    assert caught.value.how_to_fix

    with pytest.raises(MeridianError) as caught:
        claude_cli.child_environment(env_overrides={"DEEPSEEK_API_KEY": "sk-w1-probe"})
    assert caught.value.code == E_POLICY_DENIED


def test_invoke_refuses_a_credential_override_before_any_spawn(monkeypatch, tmp_path: Path) -> None:
    def poison(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("spawned with a credential-bearing child environment")

    monkeypatch.setattr(claude_cli.subprocess, "Popen", poison)
    inv = claude_cli.build_invocation(prompt="probe", workspace_root=tmp_path)
    inv.env_overrides["ANTHROPIC_API_KEY"] = "sk-ant-w1-probe"

    with pytest.raises(MeridianError) as caught:
        claude_cli.invoke(inv)
    assert caught.value.code == E_POLICY_DENIED
    assert "ANTHROPIC_API_KEY" in caught.value.message


def test_no_whitelisted_variable_is_credential_shaped() -> None:
    offenders = [name for name in claude_cli.SAFE_ENV_PASSTHROUGH if claude_cli._credential_shaped(name)]
    assert offenders == [], f"the inheritance whitelist must not carry credentials: {offenders}"


def test_an_add_dir_that_holds_a_credential_store_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MeridianError) as caught:
        claude_cli.build_invocation(
            prompt="probe",
            add_dirs=(str(claude_cli.backend_root()),),
            workspace_root=tmp_path,
        )
    assert caught.value.code == E_POLICY_DENIED
    assert "--add-dir" in caught.value.message
    assert caught.value.how_to_fix


# ── 4. an unsafe workspace ROOT is refused by name ──────────────────────────────────────────
@pytest.mark.parametrize(
    "unsafe_dir",
    ["backend", "backend/data"],
)
def test_a_workspace_root_inside_the_credential_tree_is_refused(
    monkeypatch, tmp_path: Path, unsafe_dir: str
) -> None:
    root = (
        claude_cli.backend_root() if unsafe_dir == "backend" else claude_cli.backend_root() / "data"
    )
    monkeypatch.setenv(claude_cli.CLI_WORKSPACE_ROOT_ENV, str(root))

    with pytest.raises(MeridianError) as caught:
        claude_cli.build_invocation(prompt="probe")
    assert caught.value.code == E_POLICY_DENIED
    assert "credential" in caught.value.message.lower()
    assert caught.value.how_to_fix
    assert claude_cli.CLI_WORKSPACE_ROOT_ENV in caught.value.how_to_fix
    assert Path(str(caught.value.context["cwd"])).is_relative_to(root.resolve())


def test_a_safe_custom_root_is_accepted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(claude_cli.CLI_WORKSPACE_ROOT_ENV, str(tmp_path))
    inv = claude_cli.build_invocation(prompt="probe")
    assert Path(str(inv.cwd)).is_relative_to(tmp_path.resolve())
    assert claude_cli.workspace_refusal(inv.cwd) is None


# ── 5. the file-tool allowlist is path-scoped ───────────────────────────────────────────────
@pytest.mark.parametrize("agent_type", ["business_planning", "code_generation"])
def test_every_file_tool_grant_is_path_scoped(agent_type: str, tmp_path: Path) -> None:
    capability = policy_firewall.AGENT_CAPABILITIES[agent_type]
    file_rules = [rule for rule in capability.allowed_tools if rule.split("(")[0] in FILE_TOOLS]

    assert file_rules, f"{agent_type} must still hold a Read grant (the plan runs on it)"
    for rule in file_rules:
        assert "(" in rule and rule.endswith(")"), f"AC-02: bare file-tool grant {rule!r}"
        assert "./**" in rule, f"{rule!r} must be anchored to the run workspace"
    assert not [rule for rule in capability.allowed_tools if rule in FILE_TOOLS], (
        "a bare Read/Edit/Write rule is the defect"
    )

    inv = claude_cli.build_invocation(
        prompt="probe", allowed_tools=capability.allowed_tools, workspace_root=tmp_path
    )
    assert "--allowedTools" in inv.argv
    argv_value = inv.argv[inv.argv.index("--allowedTools") + 1]
    granted = argv_value.split(",")
    assert set(granted).isdisjoint(FILE_TOOLS), f"bare file tool in the argv: {granted}"


def test_the_read_only_agent_is_not_granted_edit_or_write(tmp_path: Path) -> None:
    rules = policy_firewall.AGENT_CAPABILITIES["business_planning"].allowed_tools
    assert not [rule for rule in rules if rule.split("(")[0] in ("Edit", "Write")]
    denied = policy_firewall.AGENT_CAPABILITIES["business_planning"].denied_tools
    assert {"Edit", "Write"} <= set(denied)


def test_the_decision_leg_still_answers_for_a_read_tool_call() -> None:
    """Path scoping must not change the firewall's TOOL-name answer (it matches the name)."""
    decision = policy_firewall.decide(
        policy_firewall.ActionRequest(
            agent_type="business_planning",
            action_class=policy_firewall.ActionClass.READ,
            channel="",
            tool="Read",
        )
    )
    assert decision.decision is policy_firewall.Decision.ALLOW
