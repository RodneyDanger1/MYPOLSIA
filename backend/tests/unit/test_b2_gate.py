"""Wave B2 / seat C3 — the tool gate, approval redemption, and the policy carries.

What this file pins, one leg per item (each leg encodes the POST-fix behaviour; running it on
HEAD is the premise measurement):

  * A2A8-01 — `tool_registry.execute_tool` must hand the firewall the REAL call: channel, payload,
    taint and the run's counts.  A tainted run's write is refused, a clean approved one runs.
  * A2A9-01 — every `ActionRequest` the registry builds threads the run's taint; the census walks
    the module's AST, so a future gate call that passes `None` fails here.
  * A2A9-02 — an approval minted for a tool action is redeemable by THAT action, and replaying it
    against a different payload is refused.
  * F-29 — `dry_run` on an external call is a labelled draft (nothing sent, allowed under
    SANDBOX_MODE), while a REAL mutation is refused there and never reaches the runner.
  * F-34 — a bare verification code ("482913", no keyword) cannot ride a message body out of
    `phone_read`; withholding is decided before serialisation, by shape/value, not only by the
    keyword pattern — and ordinary prose with digits still passes.
  * F-30 — `from_branch` is ONE predicate at both seams: absent and empty string are the same
    (read), a real base is a create.
  * B2-C3-R1 — `policy_firewall` uses the shared `app.core.errors.E_RUN_CANCELLED`, not a literal.
  * B2-C3-R2 — a refused `.delay()` leaves the re-queued row `pending` with `metadata.rearmed_at`
    (the reconciler's marker) and answers with a named `E_REDIS` refusal, mirroring agents.py.

Doubles here are the tree's own labelled seams: `tool_registry._RUNNERS` (the gate is what is
judged, not GitHub), `github_service.get_branch/create_branch/commit_files` (socket boundary),
`run_agent_task.delay` (broker), and a REAL on-disk Phone Link store built by
`tests/fixtures/phone_store.py`.
"""
from __future__ import annotations

import ast
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.agents import policy_firewall as pf
from app.agents import tool_registry as registry
from app.agents.policy_firewall import ActionClass, Decision, RunApproval, RunTaint
from app.config import settings
from app.core import errors
from app.services import github_service
from tests.fixtures.phone_store import THREAD_DANA, build_phone_store, filetime

SECRET = b"c3-toolgate-secret-not-a-real-key"
REPO = "acme/widgets"
COMMIT_RESULT = {
    "ok": True, "tool": "github_commit",
    "result": {"repo": REPO, "branch": "main", "commit_sha": "sha-c3", "url": "https://x.test/c",
               "files": [], "dry_run": False},
}
#: A REAL commit request: `dry_run: false` is what the schema tells the model to send.
COMMIT_ARGS = {
    "repo": REPO, "branch": "main", "message": "tighten the gate",
    "files": [{"path": "app.py", "content": "print('hi')\n"}], "dry_run": False,
}
NOW = datetime(2026, 6, 1, 14, 0, 0)


# ── isolation ──────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_run_scope():
    pf.reset_run_taint()
    pf.reset_run_approval()
    pf.reset_run_channel_sends()
    yield
    pf.reset_run_taint()
    pf.reset_run_approval()
    pf.reset_run_channel_sends()


@pytest.fixture()
def counts():
    pf.set_run_channel_sends({"github": {"today": 0, "hour": 0}})


@pytest.fixture()
def real_sends(monkeypatch):
    """The gate's external legs are only reachable with the sandbox off (F-29 has its own leg)."""
    monkeypatch.setattr(settings, "sandbox_mode", False)


class _Runner:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[dict] = []
        self._result = dict(result if result is not None else COMMIT_RESULT)

    def __call__(self, *, agent_type: str, arguments: dict) -> dict:
        self.calls.append({"agent_type": agent_type, "arguments": arguments})
        return dict(self._result)


def _install(monkeypatch, name: str = "github_commit", result: dict | None = None) -> _Runner:
    runner = _Runner(result)
    monkeypatch.setitem(registry._RUNNERS, name, runner)
    return runner


def _call(name: str, arguments: dict | None = None, *, agent_type: str = "code_generation") -> dict:
    return json.loads(registry.execute_tool(name, arguments or {}, agent_type=agent_type))


def _record_decide(monkeypatch) -> list[pf.ActionRequest]:
    seen: list[pf.ActionRequest] = []
    real = registry.decide

    def spy(req, **kwargs):
        seen.append(req)
        return real(req, **kwargs)

    monkeypatch.setattr(registry, "decide", spy)
    return seen


# ── A2A8-01: the gate is handed the real call ─────────────────────────────────────────────────

def test_a2a8_01_the_gate_receives_the_real_channel_payload_and_taint(monkeypatch, counts, real_sends):
    seen = _record_decide(monkeypatch)
    taint = RunTaint(tainted=True, flagged_blocks=[0], sources=["layer:extra_context"])
    pf.set_run_taint(taint)
    pf.set_run_approval(RunApproval(secret=SECRET))
    runner = _install(monkeypatch)

    out = _call("github_commit", COMMIT_ARGS)

    assert len(seen) == 1, "one tool call makes exactly one policy decision"
    req = seen[0]
    assert req.channel == "github", "a GitHub mutation acts on the github channel"
    assert req.payload == COMMIT_ARGS, "the fingerprint must name the EXACT arguments"
    assert req.taint is taint, "the run's published taint, not None"
    assert req.tool == "github_commit" and req.action_class is ActionClass.EXTERNAL_IRREVERSIBLE
    assert (req.sends_today, req.sends_this_hour) == (0, 0)
    assert out["ok"] is False, "a TAINTED run's write is refused"
    assert out["error"]["code"] == "E_POLICY_DENIED"
    assert out["error"]["context"]["require_token"] is True
    assert runner.calls == [], "the refused write never reached the runner"


def test_a2a8_01_a_clean_run_with_an_attached_token_runs_the_write(monkeypatch, counts, real_sends):
    pf.set_run_taint(RunTaint(tainted=False))
    fp = pf.fingerprint(ActionClass.EXTERNAL_IRREVERSIBLE.value, "github", COMMIT_ARGS)
    pf.set_run_approval(RunApproval(secret=SECRET,
                                    token=pf.issue_approval(SECRET, action_fingerprint=fp).token))
    runner = _install(monkeypatch)

    out = _call("github_commit", COMMIT_ARGS)

    assert out["ok"] is True, out
    assert len(runner.calls) == 1 and runner.calls[0]["arguments"] == COMMIT_ARGS


# ── A2A9-01: no gate call site passes `None` ───────────────────────────────────────────────────

def test_a2a9_01_every_action_request_in_the_registry_threads_the_runs_taint():
    source = Path(registry.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    requests = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id == "ActionRequest")
             or (isinstance(node.func, ast.Attribute) and node.func.attr == "ActionRequest"))
    ]
    assert requests, "the registry must build at least one ActionRequest"
    for call in requests:
        keywords = {kw.arg for kw in call.keywords}
        assert "taint" in keywords, f"line {call.lineno}: gate call omits the run's taint"
        taint_kw = next(kw for kw in call.keywords if kw.arg == "taint")
        assert not (isinstance(taint_kw.value, ast.Constant) and taint_kw.value.value is None), (
            f"line {call.lineno}: the taint is hard-coded None")
    assert "current_run_taint" in source, "the taint must be read from the run's publisher"


def test_a2a9_01_the_read_taint_is_the_published_object(monkeypatch, real_sends):
    seen = _record_decide(monkeypatch)
    taint = RunTaint(tainted=False, sources=["memory"])
    pf.set_run_taint(taint)

    _call("pricing_catalog", {"limit": 2}, agent_type="orchestrator")

    assert seen[0].taint is taint


# ── A2A9-02: approve -> execute -> replay refused ─────────────────────────────────────────────

def test_a2a9_02_an_approval_runs_the_tool_action_and_its_replay_on_another_payload_is_refused(
        monkeypatch, counts, real_sends):
    runner = _install(monkeypatch)
    pf.set_run_approval(RunApproval(secret=SECRET))

    first = _call("github_commit", COMMIT_ARGS)
    assert first["ok"] is False and runner.calls == []
    fp = first["error"]["context"]["action_fingerprint"]
    assert first["error"]["context"]["require_token"] is True

    pf.set_run_approval(RunApproval(
        secret=SECRET, token=pf.issue_approval(SECRET, action_fingerprint=fp).token))

    approved = _call("github_commit", COMMIT_ARGS)
    assert approved["ok"] is True, approved
    assert len(runner.calls) == 1, "the approved action runs once"

    # The SAME token, a DIFFERENT commit: the approval did not cover this payload.
    other = dict(COMMIT_ARGS, message="ship something else")
    replay = _call("github_commit", other)
    assert replay["ok"] is False, "a token cannot authorise a payload it was not minted for"
    assert replay["error"]["context"]["require_token"] is True
    assert replay["error"]["context"]["action_fingerprint"] != fp
    assert len(runner.calls) == 1, "the replay never reached the runner"


# ── F-29: dry_run is a labelled draft; a real mutation is refused in the sandbox ──────────────

def test_f29_a_dry_run_external_call_is_a_labelled_draft_under_sandbox(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    plan_calls: list[dict] = []

    def plan(*args, **kwargs):
        plan_calls.append(kwargs)
        return {"repo": REPO, "branch": "main", "commit_sha": None, "url": None,
                "files": [], "dry_run": True}

    monkeypatch.setattr(github_service, "commit_files", plan)

    out = _call("github_commit", {key: value for key, value in COMMIT_ARGS.items()
                                  if key != "dry_run"})

    assert out["ok"] is True, f"a dry_run plan must not be sandbox-refused: {out}"
    assert out["result"]["dry_run"] is True, "the answer is a LABELLED draft"
    assert out["result"]["commit_sha"] is None
    assert plan_calls and plan_calls[0]["dry_run"] is True


def test_f29_a_real_mutation_is_refused_under_sandbox_and_never_runs(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    sent: list[dict] = []
    monkeypatch.setattr(github_service, "commit_files",
                        lambda *args, **kwargs: sent.append(kwargs) or {})

    out = _call("github_commit", COMMIT_ARGS)

    assert out["ok"] is False, "an irreversible external call is refused under SANDBOX_MODE"
    assert out["error"]["context"]["policy_code"] == "MERIDIAN-E114"
    assert "SANDBOX_MODE" in out["error"]["message"]
    assert sent == [], "nothing was sent"


def test_f29_a_reversible_external_action_is_a_labelled_draft_under_sandbox():
    """The firewall's own answer for a reversible action: a sandboxed DENY that is a DRAFT.

    `PolicyDecision.sandboxed` is the label the caller keeps (`e_code` is `E_SANDBOX_BLOCKED`),
    so a reversible action is never a hard policy denial — only an irreversible one must be.
    """
    decision = pf.decide(
        pf.ActionRequest(agent_type="social_media", action_class=ActionClass.EXTERNAL_REVERSIBLE,
                         channel="twitter", payload={"body": "a draft"}),
        sandbox_mode=True,
    )

    assert decision.decision is Decision.DENY
    assert decision.sandboxed is True and decision.code == "MERIDIAN-E114"
    assert decision.e_code == "E_SANDBOX_BLOCKED"


# ── F-34: a bare code cannot ride a body out of phone_read ────────────────────────────────────

@pytest.fixture()
def phone_store(tmp_path, monkeypatch):
    root = build_phone_store(tmp_path / "Database", now=NOW)
    monkeypatch.setattr(settings, "phone_link_db_dir", str(root))
    con = sqlite3.connect(root / "phone.db")
    try:
        # A BARE code: the service's keyword pattern has nothing to anchor on, so the old
        # pattern-only withholding passed it straight through to the model.
        con.execute(
            'INSERT INTO "message"(message_id, thread_id, status, from_address, type, subject, '
            "body, timestamp, pc_status, subscription_id, checksum, group_id, to_address) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (9901, THREAD_DANA, 1, "+15155550101", 1, None, "482913",
             filetime(NOW - timedelta(minutes=1)), 0, -1, 0, None, ""),
        )
        # The control: ordinary prose that CONTAINS digits is not a code and must pass verbatim.
        con.execute(
            'INSERT INTO "message"(message_id, thread_id, status, from_address, type, subject, '
            "body, timestamp, pc_status, subscription_id, checksum, group_id, to_address) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (9902, THREAD_DANA, 1, "+15155550101", 1, None, "Order 12345 shipped",
             filetime(NOW - timedelta(minutes=2)), 0, -1, 0, None, ""),
        )
        con.commit()
    finally:
        con.close()
    return root


def test_f34_a_bare_verification_code_cannot_reach_the_model(phone_store):
    raw = registry.execute_tool("phone_read", {"thread_id": THREAD_DANA, "limit": 10},
                                agent_type="orchestrator")

    assert "482913" not in raw, "a bare code is a verification code even without the keyword"
    payload = json.loads(raw)
    texts = [message["text"] for message in payload["result"]["messages"]]
    assert registry.PHONE_CODE_WITHHELD in texts


def test_f34_ordinary_prose_with_digits_still_passes(phone_store):
    payload = json.loads(registry.execute_tool(
        "phone_read", {"thread_id": THREAD_DANA, "limit": 10}, agent_type="orchestrator"))

    texts = [message["text"] for message in payload["result"]["messages"]]
    assert any("Order 12345 shipped" in text for text in texts), (
        "withholding must be a code guarantee, not a blanket redaction of digits")


# ── F-30: one predicate for `from_branch` at both seams ──────────────────────────────────────

@pytest.fixture()
def branch_calls(monkeypatch):
    calls = {"get": [], "create": []}
    monkeypatch.setattr(github_service, "get_branch",
                        lambda repo, branch, **kw: calls["get"].append((repo, branch)) or {"name": branch})
    monkeypatch.setattr(
        github_service, "create_branch",
        lambda repo, branch, from_ref, **kw: calls["create"].append((repo, branch, from_ref, kw))
        or {"name": branch, "from": from_ref, "dry_run": True})
    return calls


def test_f30_an_empty_from_branch_is_a_read_at_the_gate_and_at_the_runner(
        monkeypatch, branch_calls, real_sends):
    seen = _record_decide(monkeypatch)

    out = registry._execute(
        "github_branch",
        {"repo": REPO, "branch": "feature", "from_branch": ""},
        agent_type="code_generation",
    )

    assert seen[0].action_class is ActionClass.READ, "an empty base is not a create at the gate"
    assert out["ok"] is True, out
    assert branch_calls["create"] == [], "the runner must not send a create the gate called a read"
    assert branch_calls["get"] == [(REPO, "feature")]


def test_f30_a_missing_from_branch_is_the_same_read(branch_calls, real_sends):
    out = registry._execute("github_branch", {"repo": REPO, "branch": "feature"},
                            agent_type="code_generation")

    assert out["ok"] is True, out
    assert branch_calls["create"] == [] and branch_calls["get"] == [(REPO, "feature")]


def test_f30_a_real_base_is_a_create_at_both_seams(branch_calls, real_sends):
    out = registry._execute(
        "github_branch", {"repo": REPO, "branch": "feature", "from_branch": "main"},
        agent_type="code_generation")

    assert out["ok"] is True, out
    assert branch_calls["get"] == []
    assert [call[2] for call in branch_calls["create"]] == ["main"], "the base is the runner's"


# ── B2-C3-R1: the cancelled-run code is the shared constant ──────────────────────────────────

def test_r1_policy_firewall_imports_the_shared_cancelled_code():
    tree = ast.parse(Path(pf.__file__).read_text(encoding="utf-8"))
    assigned = {
        target.id
        for node in tree.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }
    assert "E_RUN_CANCELLED" not in assigned, "the local literal shadows the shared constant"
    imported = {
        alias.name
        for node in tree.body if isinstance(node, ast.ImportFrom)
        and node.module == "app.core.errors"
        for alias in node.names
    }
    assert "E_RUN_CANCELLED" in imported, "the code must come from app.core.errors"


def test_r1_the_cancelled_decision_maps_to_the_shared_code():
    decision = pf.PolicyDecision(Decision.DENY, "lost the claim", pf._CANCELLED_POLICY_CODE)

    assert decision.e_code == errors.E_RUN_CANCELLED
    assert pf.E_RUN_CANCELLED == errors.E_RUN_CANCELLED


# ── B2-C3-R2: a refused dispatch is not a terminal row ───────────────────────────────────────

@pytest.fixture(autouse=True)
def _quiet_redis(monkeypatch):
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


@pytest_asyncio.fixture
async def approval_client(queue_db, monkeypatch):
    from app.core.database import get_db
    from app.main import app

    monkeypatch.setattr(settings, "api_key", "test-key")

    async def _get_db():
        async with queue_db.factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def _create_task(queue_db, *, status: str) -> int:
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(db, title="c3 parked", agent_type="finance",
                                     metadata={}, status=status)
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


@pytest.mark.asyncio
async def test_r2_a_refused_dispatch_leaves_the_row_pending_and_says_not_dispatched(
        queue_db, approval_client, auth_headers, monkeypatch):
    import celery_app.tasks.agent_tasks as agent_tasks

    monkeypatch.setattr(settings, "approval_secret", "c3-secret")
    task_id = _create_task(queue_db, status="awaiting_approval")

    def dead_delay(*_args, **_kwargs):
        raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", dead_delay, raising=False)

    resp = await approval_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": "a" * 24, "task_id": task_id},
        headers=auth_headers,
    )

    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_REDIS"
    assert body["error"]["context"]["task_id"] == task_id
    assert "NOT dispatched" in body["error"]["message"]
    assert body["error"]["how_to_fix"], "a refusal a human reads carries its own fix"

    row = queue_db.row(task_id)
    assert row["status"] == "pending", (
        "a publish refusal cannot prove nothing was delivered: the row stays claimable")
    assert "E_REDIS" in (row["error_message"] or "")
    stored = json.loads(row["metadata"])
    assert stored["approval_token"], "the attach survived"
    assert stored.get("rearmed_at"), "the stranded-row reconciler's marker"
