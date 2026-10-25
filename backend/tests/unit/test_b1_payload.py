"""Wave B1 / D1 — the approval token binds the FULL payload, and `TaskResponse` serves the plan.

Two half-landed A3 findings are closed here, and this file is the pin for both.

1. A2A4-02 (P1) — `policy_firewall.fingerprint()` was made canonical/injective by S2, but every
   one of the four approval-relevant egress sites still truncated the payload BEFORE the
   fingerprint saw it: `social_media` hashed `content[:200]`, `email_outreach` hashed
   `subject[:200]` with the body unbound, `customer_support` hashed `{action, ref}` with the body
   unbound and `code_generation` hashed `{action, repo, branch, pr_title}` with the PR body
   unbound.  Measured consequence (A4 seat): two 280-char posts agreeing on chars 0-199 share ONE
   token, and a CS token for `inbox-7` authorises ANY reply body.

   The pins below are behavioural, not shape assertions — they go through the REAL agents and the
   REAL firewall (no spy on `request_side_effect`), so they cannot be satisfied by a test double:
     * two bodies that agree on their first 239 chars produce DIFFERENT fingerprints;
     * a token minted for body A lets the SAME body A through end to end (performed=True);
     * the SAME token does NOT authorise body B (the run stays parked: require_approval).
   Before the fix all four sites fail the first leg, and leg three is the exploit: the token
   verifies against the truncated/unbound payload, so body B is ALLOWED.

2. S7-R1 / A5-02 (P2) — `TaskResponse` had no `plan` / `approval_required`, so the plan section the
   UI renders had nothing to read.  The ORM attribute is `metadata_json` (the column is named
   `metadata`, which pydantic's `from_attributes` lookup resolves to Declarative's `MetaData` object
   and raises ValidationError on EVERY row — measured by the A5 seat).  The pins below validate a
   real ORM `Task` instance (the failing path) and read the route's JSON body, for a row with a
   plan, a row with no metadata, a row whose plan recorded a CYCLE, and a parked row with a pending
   approval.  Neither `metadata` nor `metadata_json` (and never `approval_token`) may reach the wire.
"""
from __future__ import annotations

from typing import Any

import pytest

import app.config as app_config
from app.agents.code_generation.agent import CodeGenerationAgent
from app.agents.customer_support.agent import CustomerSupportAgent
from app.agents.email_outreach.agent import EmailOutreachAgent
from app.agents.policy_firewall import (
    ActionClass,
    RunApproval,
    RunTaint,
    fingerprint,
    issue_approval,
    reset_run_approval,
    reset_run_taint,
    set_run_approval,
    set_run_taint,
    verify_approval,
)
from app.agents.social_media.agent import SocialMediaAgent
from app.schemas.task import TaskResponse

#: The operator's approval secret for these runs (never a real one — it only signs in-test tokens).
SECRET = b"b1-d1-approval-secret"

#: Two bodies that are BYTE-IDENTICAL for the first 239 characters and differ at char 240 — i.e.
#: identical in `content[:200]`, which is exactly what the four sites used to hash.
BODY_A = "z" * 239 + "A" + "z" * 20
BODY_B = "z" * 239 + "B" + "z" * 20

#: Measured channel counts. A `None` count makes the firewall refuse a capped channel by name
#: (E114, "no local record"), which would mask the decision under test — so the counts are real
#: zeros here, the same snapshot the worker hands a run.
_CONTEXT: dict[str, Any] = {
    "channel_sends": {
        "twitter": {"today": 0, "hour": 0},
        "email": {"today": 0, "hour": 0},
        "github": {"today": 0, "hour": 0},
    }
}


# --------------------------------------------------------------------------- the four egress sites


def _social_site(*, body: str, subject: str) -> tuple[Any, dict]:
    payload = {"action": "post", "posts": [{"platform": "twitter", "content": body}]}
    agent = SocialMediaAgent()
    agent.collect_structured = lambda task, context, **kw: (payload, None)
    return agent, payload


def _email_site(*, body: str, subject: str) -> tuple[Any, dict]:
    payload = {"action": "send", "emails": [{
        "prospect_email": "prospect@example.com", "subject": subject, "body": body,
    }]}
    agent = EmailOutreachAgent()
    agent.collect_structured = lambda task, context, **kw: (payload, None)
    return agent, payload


def _support_site(*, body: str, subject: str) -> tuple[Any, dict]:
    payload = {"replies": [{
        "message_ref": "inbox-7", "proposed_action": "send_reply",
        "from": "customer@example.com", "subject": subject, "reply_body": body,
    }]}
    agent = CustomerSupportAgent()
    agent.collect_structured = lambda task, context, **kw: (payload, None)
    return agent, payload


def _code_site(*, body: str, subject: str) -> tuple[Any, dict]:
    payload = {"action": "open_pr", "changes": [{
        "repo": "acme/site", "branch": "feat/x", "pr_title": "a real title", "pr_body": body,
    }]}
    agent = CodeGenerationAgent()
    agent.collect_structured = lambda task, context, **kw: (payload, None)
    return agent, payload


_SITES = {
    "social_media": _social_site,
    "email_outreach": _email_site,
    "customer_support": _support_site,
    "code_generation": _code_site,
}

#: (site, the payload field the two bodies differ in). Every one of these is a field the token
#: must bind; `subject` is the email site's OTHER truncated field (`subject[:200]` pre-fix).
_CASES = [
    ("social_media", "body"),
    ("email_outreach", "body"),
    ("email_outreach", "subject"),
    ("customer_support", "body"),
    ("code_generation", "body"),
]


def _stub_providers(monkeypatch) -> None:
    """The PROVIDER side of an allowed action (no network in a unit test).

    These are the third-party integrations the agent calls only AFTER the firewall ALLOWed the
    action — the code under test (the agent's payload construction and the firewall) is not
    stubbed anywhere in this file.
    """
    monkeypatch.setattr(
        "app.agents.social_media.agent.integrations.post_tweet",
        lambda content, platform="", **kw: {"ok": True, "result": {"id": "tw-real-1"}},
    )
    monkeypatch.setattr(
        "app.agents.email_outreach.agent.send_outreach_email",
        lambda **kw: {"ok": True, "result": {"message_id": "sg-real-1"}},
    )
    monkeypatch.setattr(
        "app.agents.customer_support.agent.send_cs_reply",
        lambda **kw: {"ok": True, "result": {"message_id": "sg-real-2"}},
    )
    monkeypatch.setattr(
        "app.agents.code_generation.agent.integrations.open_pull_request",
        lambda **kw: {"ok": True,
                      "result": {"url": "https://github.com/acme/site/pull/7", "number": 7}},
    )


def _run_site(site: str, monkeypatch, *, body: str, subject: str = "a subject",
              token: str = "") -> dict:
    """One agent run, outside sandbox, in a TAINTED run with the operator's approval channel on.

    Taint is the shipped way a reversible action is escalated to a human (policy_firewall
    `decide()`), so this exercises the same REQUIRE_APPROVAL -> token path the production worker
    drives — without inventing a gate the runtime does not have.
    """
    agent, payload = _SITES[site](body=body, subject=subject)
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    set_run_taint(RunTaint(tainted=True, flagged_blocks=[1], sources=["task"]))
    set_run_approval(RunApproval(secret=SECRET, token=token))
    try:
        out = agent.run({"title": "gated egress"}, dict(_CONTEXT))
    finally:
        reset_run_taint()
        reset_run_approval()
    assert isinstance(out, dict), out
    assert out.get("side_effects") == payload.get("side_effects"), (
        "the run must publish the receipt of the ONE attempted action"
    )
    return out


def _receipt(out: dict) -> dict:
    receipts = [r for r in (out.get("side_effects") or []) if isinstance(r, dict)]
    assert len(receipts) == 1, f"expected exactly one receipt, got {receipts!r}"
    return receipts[0]


def _fingerprint_of(site: str, monkeypatch, *, body: str, subject: str = "a subject") -> str:
    receipt = _receipt(_run_site(site, monkeypatch, body=body, subject=subject))
    assert receipt.get("decision") == "require_approval", (
        f"{site}: the gate must park the action for a human; receipt={receipt!r}"
    )
    fp = str(receipt.get("fingerprint") or "")
    assert fp, f"{site}: the parked receipt carries no fingerprint: {receipt!r}"
    return fp


@pytest.mark.parametrize("site,field", _CASES)
def test_a_body_differing_past_char_200_gets_a_different_fingerprint(site, field, monkeypatch):
    """A2A4-02, first direction: the token must bind the body that will ACTUALLY be sent.

    The two payloads agree on their first 239 characters and differ at char 240 — inside the
    `[:200]` truncation window of the old call sites, so pre-fix both runs hashed the same
    preimage and produced ONE fingerprint.
    """
    body_a, body_b = BODY_A, BODY_B
    subject_a = BODY_A if field == "subject" else "a subject"
    subject_b = BODY_B if field == "subject" else "a subject"

    fp_a = _fingerprint_of(site, monkeypatch, body=body_a, subject=subject_a)
    fp_b = _fingerprint_of(site, monkeypatch, body=body_b, subject=subject_b)
    assert fp_a != fp_b, (
        f"{site}: two payloads differing only at char 240 of {field!r} produced the SAME "
        f"approval fingerprint ({fp_a}) — the token does not bind the payload that is sent"
    )


@pytest.mark.parametrize("site,field", _CASES)
def test_one_token_authorises_its_own_body_and_never_a_different_one(site, field, monkeypatch):
    """A2A4-02, both directions, end to end through the real agent and the real firewall.

    * the SAME action re-verified still passes (an unchanged action is never re-gated);
    * the same token does NOT authorise a payload that differs past char 200.
    """
    _stub_providers(monkeypatch)
    body_a, body_b = BODY_A, BODY_B
    subject_a = BODY_A if field == "subject" else "a subject"
    subject_b = BODY_B if field == "subject" else "a subject"

    fp_a = _fingerprint_of(site, monkeypatch, body=body_a, subject=subject_a)
    token = issue_approval(SECRET, action_fingerprint=fp_a).token
    assert verify_approval(SECRET, token, action_fingerprint=fp_a) is True

    # (1) the operator approved THIS action: re-verifying it must let it through.
    same = _receipt(_run_site(site, monkeypatch, body=body_a, subject=subject_a, token=token))
    assert same.get("performed") is True, (
        f"{site}: the approved action was re-gated — an unchanged action must still verify: "
        f"{same!r}"
    )
    assert same.get("decision") == "allow", same

    # (2) the SAME token on a body that differs past char 200 must NOT be authorised.
    other = _receipt(_run_site(site, monkeypatch, body=body_b, subject=subject_b, token=token))
    assert other.get("performed") is False, (
        f"{site}: a token minted for one {field} authorised a DIFFERENT one — the fingerprint "
        f"does not bind the payload that is sent: {other!r}"
    )
    assert other.get("decision") == "require_approval", other
    assert other.get("fingerprint") != fp_a


def test_the_github_payload_binds_the_full_pr_body_and_every_target_id(monkeypatch):
    """The exact preimage the open-PR gate hashes: action + repo + branch + pr_title + FULL body.

    `code_generation` is the irreversible site (approval ALWAYS required), so its preimage is the
    one every operator-visible fingerprint is minted against. Pinned literally: a field silently
    dropped from this dict would quietly un-bind that part of the action.
    """
    expected = {
        "action": "open_pr",
        "repo": "acme/site",
        "branch": "feat/x",
        "pr_title": "a real title",
        "pr_body": BODY_A,
    }
    assert _fingerprint_of("code_generation", monkeypatch, body=BODY_A) == fingerprint(
        ActionClass.EXTERNAL_IRREVERSIBLE.value, "github", expected
    )


# ------------------------------------------------------------------- TaskResponse: plan + approval


def _orm_task(**overrides: Any):
    """An UNSAVED ORM `Task` — the exact object `TaskResponse.model_validate` sees in the route.

    This is the failing path the A5 seat measured: pydantic's `from_attributes` lookup on a
    SQLAlchemy instance finds `Task.metadata` (Declarative's `MetaData` object), so a response
    field literally named `metadata` raises ValidationError on EVERY row.
    """
    from app.models.task import Task

    values: dict[str, Any] = {
        "id": 11,
        "title": "write the launch post",
        "agent_type": "social_media",
        "status": "pending",
        "priority": 3,
        "source": "orchestrator",
    }
    values.update(overrides)
    return Task(**values)


#: What `daily_cycle._morning_cycle` persists on the row (celery_app/tasks/daily_cycle.py:619-628)
#: plus the second pass's resolved ids and the cycle marker (:673-710).
_PLAN_METADATA = {
    "plan_day": "2026-10-24",
    "depends_on_titles": ["Research the niche"],
    "depends_on_ids": [7],
    "side_effect": "external_reversible",
    "acceptance": "a draft exists and is reviewable",
    "estimate_usd": 0.25,
    "estimate_seconds": 300,
}


def test_the_plan_field_reads_the_metadata_column_off_an_orm_row() -> None:
    """plan = the persisted edge: ids for the DAG, the titles it was resolved from, no cycle."""
    response = TaskResponse.model_validate(_orm_task(metadata_json=_PLAN_METADATA))

    assert response.plan is not None
    assert response.plan["depends_on"] == [7], "the resolved ids are the DAG edges"
    assert response.plan["depends_on_cycle"] is False
    assert response.plan["depends_on_titles"] == ["Research the niche"]
    assert response.plan["side_effect"] == "external_reversible"
    assert response.plan["acceptance"] == "a draft exists and is reviewable"
    assert response.plan["estimate_usd"] == 0.25
    assert response.plan["estimate_seconds"] == 300
    assert response.plan["plan_day"] == "2026-10-24"
    assert response.approval_required is False


def test_a_task_with_no_metadata_serves_no_plan_and_no_approval() -> None:
    """Absence is absence: a hand-made row has no plan edge and nothing parked on it."""
    response = TaskResponse.model_validate(_orm_task(metadata_json=None))

    assert response.plan is None
    assert response.approval_required is False


def test_a_plan_that_recorded_a_cycle_is_flagged_and_has_no_edges() -> None:
    """`daily_cycle.py:700-705` releases a stranded node as a root: ids=[] plus the cycle titles.

    The response must say so (a client that read the empty edge list as "no dependencies" would
    be stating something false), and the release decision must stay visible.
    """
    metadata = {
        "plan_day": "2026-10-24",
        "depends_on_titles": ["Task B"],
        "depends_on_ids": [],
        "depends_on_cycle": ["Task B"],
    }
    response = TaskResponse.model_validate(_orm_task(metadata_json=metadata))

    assert response.plan is not None
    assert response.plan["depends_on"] == []
    assert response.plan["depends_on_cycle"] is True
    assert response.plan["depends_on_titles"] == ["Task B"]


def test_a_parked_row_carries_a_pending_approval() -> None:
    """`agent_tasks.APPROVAL_REQUIRED_KEY` (agent_tasks.py:553, written at :1094).

    The list is what the operator mints a token for; the boolean is the flag the surfaces gate on.
    An EMPTY list is a cleared park, not a pending one.
    """
    pending = [{
        "action": "open_pr",
        "ref": "acme/site#feat/x",
        "channel": "github",
        "fingerprint": "0f1e2d3c4b5a6978",
        "reason": "irreversible action",
    }]
    parked = TaskResponse.model_validate(_orm_task(
        metadata_json={"approval_required": pending}, status="awaiting_approval",
    ))
    assert parked.approval_required is True
    assert parked.approval_fingerprints == ["0f1e2d3c4b5a6978"]

    cleared = TaskResponse.model_validate(_orm_task(metadata_json={"approval_required": []}))
    assert cleared.approval_required is False
    assert cleared.approval_fingerprints == []


def test_the_metadata_bag_and_the_approval_token_never_reach_the_wire() -> None:
    """`metadata.approval_token` is a CREDENTIAL the worker reads (task_service.py:347).

    So the narrowed fields are the ONLY view of the bag: the raw dict (and the column's own name)
    must not appear, and neither may the token, in any serialisation the API can produce.
    """
    metadata = {**_PLAN_METADATA, "approval_token": "exp.signature", "approval_required": [{
        "fingerprint": "0f1e2d3c4b5a6978", "channel": "github",
    }]}
    response = TaskResponse.model_validate(_orm_task(metadata_json=metadata))

    for mode in ("python", "json"):
        body = response.model_dump(mode=mode)
        assert "metadata" not in body, f"{mode}: the reserved name must never be a response key"
        assert "metadata_json" not in body, f"{mode}: the column accessor is internal"
        assert "exp.signature" not in str(body), f"{mode}: the approval token leaked onto the wire"
        assert body["plan"]["depends_on"] == [7]
        assert body["approval_required"] is True

    # The token is dropped from the fingerprint list too: only fingerprints are served.
    assert response.approval_fingerprints == ["0f1e2d3c4b5a6978"]


def test_a_non_object_metadata_bag_serves_instead_of_killing_the_list(caplog) -> None:
    """`tasks.metadata` is an unconstrained JSON column: one list value must not 500 every row.

    The L1-11 class: a schema-legal row whose JSON value is not an object is read as "no plan,
    nothing parked" (both true of that row) and the drop is NAMED in the log.
    """
    response = TaskResponse.model_validate(_orm_task(metadata_json=["not", "a", "bag"]))

    assert response.plan is None
    assert response.approval_required is False
    assert response.approval_fingerprints == []
    assert any("task_metadata_not_an_object" in record.getMessage() for record in caplog.records), (
        "a dropped metadata bag must be recorded by name, never silently ignored"
    )


def test_the_serialised_schema_serves_the_computed_fields_and_not_the_bag() -> None:
    """The documented wire: the three computed fields are IN, `metadata`/`metadata_json` are OUT."""
    props = TaskResponse.model_json_schema(mode="serialization")["properties"]

    assert "plan" in props
    assert "approval_required" in props and props["approval_required"]["type"] == "boolean"
    assert "approval_fingerprints" in props
    assert "metadata" not in props, "the reserved name must never be a documented response key"
    assert "metadata_json" not in props, "the column accessor is internal (exclude=True)"


@pytest.mark.asyncio
async def test_the_tasks_route_serves_the_plan_and_the_parked_flag(
    api_client, auth_headers, async_db_session
) -> None:
    """The wire, not the model: `GET /api/v1/tasks/{id}` (and the list) after a real row write."""
    from app.services.task_service import create_task

    planned = await create_task(
        async_db_session, title="write the launch post", agent_type="social_media",
        metadata=dict(_PLAN_METADATA),
    )
    parked = await create_task(
        async_db_session, title="open the PR", agent_type="code_generation",
        status="awaiting_approval",
        metadata={"approval_required": [{"fingerprint": "0f1e2d3c4b5a6978", "channel": "github"}]},
    )
    await async_db_session.commit()

    detail = await api_client.get(f"/api/v1/tasks/{planned.id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text[:300]
    body = detail.json()
    assert body["plan"]["depends_on"] == [7]
    assert body["plan"]["depends_on_cycle"] is False
    assert body["approval_required"] is False
    assert "metadata" not in body and "metadata_json" not in body

    gated = await api_client.get(f"/api/v1/tasks/{parked.id}", headers=auth_headers)
    assert gated.status_code == 200, gated.text[:300]
    assert gated.json()["approval_required"] is True
    assert gated.json()["approval_fingerprints"] == ["0f1e2d3c4b5a6978"]

    listed = await api_client.get("/api/v1/tasks", headers=auth_headers)
    assert listed.status_code == 200, listed.text[:300]
    rows = {row["id"]: row for row in listed.json()}
    assert rows[planned.id]["plan"]["depends_on"] == [7]
    assert rows[parked.id]["approval_required"] is True
    for row in listed.json():
        assert "metadata" not in row and "metadata_json" not in row
