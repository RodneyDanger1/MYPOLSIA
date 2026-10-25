"""WAVE A3 / S3 — the write path: a retry must never repeat a side effect (§4.13).

Every test here runs against the REAL modules; the only interceptions are the seams the product
itself defines for tests:

  * the HTTP seam — `httpx.MockTransport` handed to `deepseek_runtime.invoke` (production builds a
    real client per call, so this is the documented injection point);
  * the registry seam — a counting stand-in installed on `deepseek_runtime._tool_registry`, the
    lazy-import edge `_execute_tool_calls` resolves at call time (same pattern as
    `tests/unit/test_v12_tool_loop.py`);
  * the agent seam — a stand-in for `crew_factory.run_agent_for_task` (what the worker calls)
    which drives the REAL `_execute_tool_calls` with the REAL replay index;
  * the broker seam — `run_agent_task.delay` / `celery.group` replaced by recorders.

What is pinned:

1. §4.13's record shape, its `call_key` identity, its result digest, and the envelope's presence
   rule (`actions` appears only on a tool-enabled run, exactly like `tools_used`);
2. a replay executes NOTHING and hands the recorded result back; an unreplayable record is a
   NAMED refusal (`E_ACTION_REPLAY_UNAVAILABLE`), never a silent re-send;
3. the in-run ladder (`claude_cli.run_with_retries`) and the Celery retry both replay the
   earlier attempt's actions — the measured 3x replay (F-13);
4. A2A4-01: the two failures the ladder retries (transport, non-200) carry `tools_used` +
   `usage` + the action list;
5. A2A8-02: a lease-lost run pushes NO owner notification;
6. A2-03: a refused hand-off RE-ARMS the rows (they stay `pending` and the reconciler sees them)
   instead of terminalising work whose message may already be queued.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime
from app.agents.base_agent import BasePolsiaAgent
from app.core.errors import E_CLI_TIMEOUT, PolsiaError
from celery_app.tasks import _common

#: Clearly fake, ASCII-only (httpx encodes header values as ASCII) — house pattern.
SAFE_TEST_KEY = "test-key-never-a-real-secret"

#: §4.13's record fields, plus `result`: the persisted answer a replay re-sends.  The digest is
#: taken over exactly that text, so an entry without one cannot be replayed (and is refused).
CONTRACT_KEYS = frozenset(
    {"call_key", "tool", "args", "run_id", "attempt", "result_digest", "status", "result"}
)

FETCH_ARGS = '{"url": "https://example.test/page"}'
FETCH_RESULT = "Fetched https://example.test — 12 chars of text."


class _ResearchAgent(BasePolsiaAgent):
    """A real subclass whose `run()` is not used: only the invoke seam is exercised."""

    agent_type = "competitor_research"

    def run(self, task, context):  # pragma: no cover - never dispatched here
        return {"summary": "unused"}


class _Registry:
    """The frozen registry seam: counts every execution and answers VERBATIM."""

    TOOL_SPECS = [
        {"type": "function", "function": {
            "name": "web_fetch",
            "description": "Fetch one http(s) URL and return its text.",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                           "required": ["url"]},
        }},
        {"type": "function", "function": {
            "name": "macro_snapshot",
            "description": "The cached macro snapshot.",
            "parameters": {"type": "object", "properties": {}},
        }},
    ]

    def __init__(self, result: str = FETCH_RESULT) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    def execute_tool(self, name, arguments, *, agent_type, **_kwargs):
        self.calls.append((name, arguments))
        return self.result


def _usage() -> dict:
    return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            "prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 8}


def _tool_call(call_id: str, name: str, arguments) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _completion(content, *, finish: str = "stop", tool_calls=None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"id": "chat-1", "object": "chat.completion", "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": _usage()}


class _Scripted:
    """A scripted provider: each request pops the next payload; an exception is RAISED."""

    def __init__(self, *payloads) -> None:
        self.queue = list(payloads)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"url": str(request.url), "body": json.loads(request.content)})
        assert self.queue, "unscripted extra provider request (the loop over-called)"
        payload = self.queue.pop(0)
        if isinstance(payload, int):
            return httpx.Response(payload, json={"error": {"message": f"status {payload}"}})
        if isinstance(payload, BaseException):
            raise payload
        return httpx.Response(200, json=payload)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def messages(self, index: int) -> list[dict]:
        return self.requests[index]["body"]["messages"]

    def tool_messages(self, index: int) -> list[dict]:
        return [m for m in self.messages(index) if m["role"] == "tool"]

    @property
    def calls(self) -> int:
        return len(self.requests)


def _inv(prompt: str = "do the thing", *, agent_type: str = "competitor_research"):
    return claude_cli.build_invocation(
        prompt=prompt,
        allowed_tools=("web_fetch",),
        timeout_s=30,
        schema_name=agent_type,
    )


def _invoke(server: _Scripted, inv, **kwargs):
    return deepseek_runtime.invoke(inv, client=server.client(), api_key=SAFE_TEST_KEY, **kwargs)


@pytest.fixture()
def registry(monkeypatch):
    reg = _Registry()
    monkeypatch.setattr(deepseek_runtime, "_tool_registry", lambda: reg, raising=False)
    return reg


@pytest.fixture(autouse=True)
def _clean_session_store():
    """The runtime's session store is process-global by design; keep each test hermetic."""
    deepseek_runtime._reset_session_store()
    yield
    deepseek_runtime._reset_session_store()


# ─────────────────────────────────────────────────────────────────────────────
# 1. §4.13's record and the envelope's presence rule
# ─────────────────────────────────────────────────────────────────────────────
def test_the_envelope_and_the_persisted_channel_share_the_actions_key():
    """One key name in two modules (the runtime's envelope, the worker's `tasks.metadata`).

    `app.agents` must not import the Celery task layer, so the literal is declared on both sides;
    this pin is what stops them drifting apart.
    """
    assert deepseek_runtime.ACTION_KEY == _common.ACTION_KEY == "actions"


def test_a_tool_less_envelope_gains_no_actions_key(registry):
    """The shipped byte-shape for tool-less runs does not move (same rule as `tools_used`)."""
    server = _Scripted(_completion("plain answer"))
    inv = claude_cli.build_invocation(prompt="hi", timeout_s=30, schema_name="competitor_research")

    envelope = json.loads(_invoke(server, inv).stdout)

    assert "tools_used" not in envelope
    assert "actions" not in envelope
    assert server.calls == 1
    assert registry.calls == []


def test_an_executed_tool_call_is_recorded_in_the_contract_shape(registry):
    """§4.13: `call_key`, `tool`, `args`, `run_id`, `attempt`, `result_digest`, `status`."""
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_fetch", FETCH_ARGS)]),
        _completion('{"summary": "used the fetch"}'),
    )

    result = _invoke(server, _inv(), run_id="42", attempt=2)
    envelope = json.loads(result.stdout)
    records = envelope["actions"]

    assert len(records) == 1
    record = records[0]
    assert set(record) == CONTRACT_KEYS, "the entry carries exactly the contract's fields + result"
    assert record["tool"] == "web_fetch"
    assert record["args"] == {"url": "https://example.test/page"}
    assert record["status"] == "executed"
    assert record["run_id"] == 42, "the run identity is the INT the column holds"
    assert record["attempt"] == 2
    assert record["call_key"] == deepseek_runtime.call_key(
        "web_fetch", {"url": "https://example.test/page"}
    )
    assert record["call_key"].startswith("web_fetch|")
    assert record["result"] == FETCH_RESULT
    assert record["result_digest"] == hashlib.sha256(FETCH_RESULT.encode("utf-8")).hexdigest()
    assert envelope["tools_used"] == ["web_fetch"]


def test_the_call_key_is_attempt_independent_and_argument_sensitive():
    """The identity a retry matches on: same tool+args -> same key, whatever the attempt."""
    key = deepseek_runtime.call_key("web_fetch", {"url": "https://a.test", "n": 1})
    assert key == deepseek_runtime.call_key("web_fetch", {"n": 1, "url": "https://a.test"}), (
        "canonical JSON: key order must not change the identity"
    )
    assert key != deepseek_runtime.call_key("web_fetch", {"url": "https://b.test", "n": 1})
    assert key != deepseek_runtime.call_key("macro_snapshot", {"url": "https://a.test", "n": 1})


def test_a_record_whose_result_exceeds_the_entry_bound_drops_the_result_not_the_identity(registry):
    """4 KB per entry: the RESULT absorbs the bound, and an entry with no result is refused later."""
    registry.result = "x" * 20_000
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_fetch", FETCH_ARGS)]),
        _completion("done"),
    )

    record = json.loads(_invoke(server, _inv()).stdout)["actions"][0]

    assert record["call_key"].startswith("web_fetch|")
    assert record["args"] == {"url": "https://example.test/page"}
    assert len(json.dumps(record, ensure_ascii=False).encode("utf-8")) <= (
        deepseek_runtime.MAX_ACTION_ENTRY_BYTES
    )
    assert len(record["result"]) < 20_000
    assert record["result_digest"] == hashlib.sha256(record["result"].encode("utf-8")).hexdigest(), (
        "the digest binds the text that was actually persisted"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. replay: execute once, answer from the record, refuse what cannot be replayed
# ─────────────────────────────────────────────────────────────────────────────
def test_a_replay_does_not_execute_the_same_call_twice(registry):
    """F-13's core: the second attempt gets the recorded answer, the effect happens ONCE."""
    first_server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_fetch", FETCH_ARGS)]),
        _completion('{"summary": "used the fetch"}'),
    )
    first = _invoke(first_server, _inv())
    recorded = json.loads(first.stdout)["actions"]
    assert len(registry.calls) == 1

    second_server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c9", "web_fetch", FETCH_ARGS)]),
        _completion('{"summary": "used the fetch again"}'),
    )
    second = _invoke(second_server, _inv(), replay=recorded)
    envelope = json.loads(second.stdout)

    assert len(registry.calls) == 1, "the replay must not reach the executor a second time"
    assert envelope["actions"][0]["status"] == "replayed"
    assert envelope["actions"][0]["call_key"] == recorded[0]["call_key"]
    # The model still gets the tool's answer — the conversation continues as if it had run.
    assert second_server.tool_messages(1)[0]["content"] == FETCH_RESULT
    assert envelope["tools_used"] == ["web_fetch"], (
        "the effect exists in the world, so the retry-suppression evidence must name the tool"
    )


def test_a_different_call_on_a_replay_attempt_still_executes(registry):
    """Only the EXACT key is replayed; a new call in the same run is real work."""
    recorded = [{
        "call_key": deepseek_runtime.call_key("macro_snapshot", {}),
        "tool": "macro_snapshot", "args": {},
        "run_id": None, "attempt": 1, "status": "executed",
        "result": "cached snapshot", "result_digest": hashlib.sha256(b"cached snapshot").hexdigest(),
    }]
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_fetch", FETCH_ARGS)]),
        _completion("done"),
    )

    envelope = json.loads(_invoke(server, _inv(), replay=recorded).stdout)

    assert [call[0] for call in registry.calls] == ["web_fetch"]
    assert envelope["actions"][0]["status"] == "executed"


def test_a_record_without_a_persisted_result_is_refused_not_re_sent(registry):
    """§4.13: a stored entry that cannot be replayed is a REFUSAL, never a silent re-send."""
    key = deepseek_runtime.call_key("web_fetch", {"url": "https://example.test/page"})
    recorded = [{"call_key": key, "tool": "web_fetch",
                 "args": {"url": "https://example.test/page"},
                 "run_id": 7, "attempt": 1, "status": "executed"}]
    server = _Scripted(_completion(None, finish="tool_calls",
                                   tool_calls=[_tool_call("c1", "web_fetch", FETCH_ARGS)]))

    with pytest.raises(PolsiaError) as excinfo:
        _invoke(server, _inv(), replay=recorded)

    assert excinfo.value.code == deepseek_runtime.E_ACTION_REPLAY_UNAVAILABLE
    assert excinfo.value.how_to_fix, "a refusal must say how to fix it"
    assert excinfo.value.context["call_key"] == key
    assert registry.calls == [], "nothing was executed"
    assert server.calls == 1, "and nothing was re-sent to the provider"


def test_a_tampered_record_is_refused_rather_than_replayed(registry):
    """The digest is the integrity binding: edited text is a refusal, not a plausible answer."""
    text = "the tool said A"
    recorded = [{
        "call_key": deepseek_runtime.call_key("web_fetch", {"url": "https://example.test/page"}),
        "tool": "web_fetch", "args": {"url": "https://example.test/page"},
        "run_id": 7, "attempt": 1, "status": "executed",
        "result": "the tool said B",  # edited after the fact
        "result_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }]
    server = _Scripted(_completion(None, finish="tool_calls",
                                   tool_calls=[_tool_call("c1", "web_fetch", FETCH_ARGS)]))

    with pytest.raises(PolsiaError) as excinfo:
        _invoke(server, _inv(), replay=recorded)

    assert excinfo.value.code == deepseek_runtime.E_ACTION_REPLAY_UNAVAILABLE
    assert registry.calls == []


def test_a_started_record_fails_closed_instead_of_re_executing(registry):
    """A `started` record means the process died mid-effect: re-running it could double the send."""
    key = deepseek_runtime.call_key("web_fetch", {"url": "https://example.test/page"})
    recorded = [{"call_key": key, "tool": "web_fetch", "args": {"url": "https://example.test/page"},
                 "run_id": 7, "attempt": 1, "status": "started"}]
    server = _Scripted(_completion(None, finish="tool_calls",
                                   tool_calls=[_tool_call("c1", "web_fetch", FETCH_ARGS)]))

    with pytest.raises(PolsiaError) as excinfo:
        _invoke(server, _inv(), replay=recorded)

    assert excinfo.value.code == deepseek_runtime.E_ACTION_REPLAY_UNAVAILABLE
    assert registry.calls == [], "an unknown outcome is never re-executed"


# ─────────────────────────────────────────────────────────────────────────────
# 3. F-13 through the REAL ladder and the REAL worker
# ─────────────────────────────────────────────────────────────────────────────
def test_the_in_run_ladder_replays_after_a_429(registry, monkeypatch):
    """The measured 3x replay: `run_with_retries` re-runs the whole invocation on a 429.

    Three dispatches, each asking for the same `web_fetch` and then hitting a 429 — except the
    last, which gets its answer.  With the run's own journal fed back as `replay`, the tool is
    executed ONCE and answered from the record on the retries.
    """
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: SAFE_TEST_KEY)
    monkeypatch.setattr(claude_cli, "backoff_delay", lambda attempt, **_kwargs: 0.0)
    tool_turn = _completion(None, finish="tool_calls",
                            tool_calls=[_tool_call("c1", "web_fetch", FETCH_ARGS)])
    server = _Scripted(
        tool_turn, 429,      # dispatch 1: executes web_fetch, then the provider refuses
        tool_turn, 429,      # dispatch 2 (ladder retry): must be a REPLAY
        tool_turn, _completion('{"summary": "used the fetched page"}'),
    )
    real_invoke = deepseek_runtime.invoke

    def observed(inv, **kwargs):
        return real_invoke(inv, client=server.client(), api_key=SAFE_TEST_KEY, **kwargs)

    monkeypatch.setattr(deepseek_runtime, "invoke", observed)

    agent = _ResearchAgent()
    raw = agent._real_call("do the thing")

    assert raw.text == '{"summary": "used the fetched page"}'
    assert len(registry.calls) == 1, "the ladder's retries must not re-execute the tool"
    assert server.calls == 6, "three dispatches, two provider calls each"
    statuses = [record["status"] for record in agent.last_actions]
    assert statuses == ["executed", "replayed", "replayed"], statuses


class _WorkerToolRun:
    """One stand-in agent run that performs ONE tool call through the REAL runtime seam.

    `task_dict["prior_actions"]` is exactly what the worker read from `tasks.metadata` at the
    claim — the same list `base_agent._real_call` hands to the runtime as `replay` — so this
    double exercises the shipped replay branch instead of describing it.
    """

    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def __call__(self, agent_type, task_dict, context):
        prior = task_dict.get("prior_actions") or []
        journal: list[dict] = []
        deepseek_runtime._execute_tool_calls(
            [_tool_call("c1", "web_fetch", FETCH_ARGS)],
            agent_type=agent_type,
            url="test://write-path",
            run_id=task_dict.get("run_id"),
            attempt=task_dict.get("attempt"),
            replay=deepseek_runtime.replay_index(prior),
            journal=journal,
        )
        self.calls.append({"prior": len(prior), "journal": journal})
        if self.fail:
            # The run FAILED after the effect: a failed run stores no output, so the refusal's
            # context is the only carrier the worker can persist.
            raise PolsiaError(E_CLI_TIMEOUT, "test-only transport failure",
                              context={"actions": journal})
        return {"summary": "used the fetch", "actions": journal}


def test_a_celery_retry_replays_instead_of_repeating_the_run(queue_db, eager_delivery, monkeypatch):
    """3 attempts, ONE execution of the tool call, one `executed` record on the task row."""
    registry = _Registry()
    monkeypatch.setattr(deepseek_runtime, "_tool_registry", lambda: registry, raising=False)
    run = _WorkerToolRun(fail=True)
    import app.agents.crew_factory as crew_factory

    monkeypatch.setattr(crew_factory, "run_agent_for_task", run)

    task_id = queue_db.create_task(title="a3 retry replay")
    result = eager_delivery(task_id)

    assert result.state == "FAILURE", "the run really failed every attempt"
    assert len(run.calls) == 3, "the ladder makes three attempts"
    assert len(registry.calls) == 1, "the tool call is executed ONCE across all three attempts"
    assert [call["prior"] for call in run.calls] == [0, 1, 1], (
        "attempt 1 had no record; attempts 2 and 3 replayed the one attempt 1 wrote"
    )
    assert run.calls[1]["journal"][0]["status"] == "replayed"

    row = queue_db.row(task_id)
    actions = json.loads(row["metadata"])["actions"]
    assert len(actions) == 1, "one effect keeps one record, however often it was replayed"
    assert actions[0]["status"] == "executed"
    assert actions[0]["call_key"].startswith("web_fetch|")
    assert row["attempts"] == 3


def test_a_completed_run_records_its_actions_on_the_task_row(queue_db, monkeypatch):
    """The success path carries the same list, so `agent_runs.output` AND the row agree."""
    registry = _Registry()
    monkeypatch.setattr(deepseek_runtime, "_tool_registry", lambda: registry, raising=False)
    run = _WorkerToolRun(fail=False)
    import app.agents.crew_factory as crew_factory

    monkeypatch.setattr(crew_factory, "run_agent_for_task", run)

    from celery_app.tasks.agent_tasks import _execute_agent_task

    task_id = queue_db.create_task(title="a3 completed run")
    outcome = _common.run_async(_execute_agent_task(task_id))

    assert outcome["status"] == "completed"
    actions = json.loads(queue_db.row(task_id)["metadata"])["actions"]
    assert len(actions) == 1 and actions[0]["status"] == "executed"
    runs = queue_db.query("SELECT output FROM agent_runs WHERE task_id = ?", (task_id,))
    assert len(runs) == 1
    assert json.loads(runs[0]["output"])["actions"][0]["call_key"] == actions[0]["call_key"]


def test_actions_from_outcome_reads_the_failure_context_and_ignores_junk():
    record = {"call_key": "web_fetch|abc", "tool": "web_fetch", "status": "executed"}
    failure = PolsiaError(E_CLI_TIMEOUT, "boom", context={"actions": [record, "not-a-record"]})

    assert _common.actions_from_outcome({}, failure) == [record]
    assert _common.actions_from_outcome({"actions": [record]}, None) == [record]
    assert _common.actions_from_outcome({}, None) == []
    assert _common.actions_from_outcome({}, ValueError("no context")) == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. A2A4-01 — the retry evidence on the two failure envelopes
# ─────────────────────────────────────────────────────────────────────────────
def test_a_429_after_tools_carries_tools_used_usage_and_the_action_list(registry):
    """A2A4-01: without this, every tool run looked TOOL-LESS at the retry seam."""
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_fetch", FETCH_ARGS)]),
        429,
    )

    envelope = json.loads(_invoke(server, _inv()).stdout)

    assert envelope["is_error"] is True
    assert envelope["api_error_status"] == 429
    assert envelope["tools_used"] == ["web_fetch"], "the failure must carry the tool evidence"
    assert envelope["usage"]["input_tokens"] == 10, "and what the loop already spent"
    assert [record["status"] for record in envelope["actions"]] == ["executed"]
    assert envelope["actions"][0]["result"] == FETCH_RESULT


def test_a_transport_fault_after_tools_carries_tools_used(registry):
    """The other retried branch: a connect/read fault mid-loop."""
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_fetch", FETCH_ARGS)]),
        httpx.ConnectError("connection refused"),
    )

    envelope = json.loads(_invoke(server, _inv()).stdout)

    assert envelope["is_error"] is True
    assert envelope["subtype"] == "error_api_error"
    assert envelope["tools_used"] == ["web_fetch"]
    assert envelope["actions"][0]["call_key"].startswith("web_fetch|")


def test_a_first_call_failure_still_says_tools_were_available(registry):
    """`tools_used == []` is a MEASURED zero before any tool turn, never an absent key."""
    server = _Scripted(500)

    envelope = json.loads(_invoke(server, _inv()).stdout)

    assert envelope["tools_used"] == []
    assert envelope["actions"] == []


# ─────────────────────────────────────────────────────────────────────────────
# 5. the list bound and the merge rules (§4.13)
# ─────────────────────────────────────────────────────────────────────────────
def _record(key: str, status: str = "executed") -> dict:
    return {"call_key": key, "tool": key.split("|")[0], "args": {}, "run_id": 1,
            "attempt": 1, "status": status, "result": key,
            "result_digest": hashlib.sha256(key.encode("utf-8")).hexdigest()}


def test_merge_bounds_the_list_and_records_what_it_evicted():
    """50 entries, 4 KB each; past the bound the OLDEST goes and ONE summary row counts them."""
    records = [_record(f"web_fetch|{index:04d}") for index in range(60)]

    merged = _common.merge_actions([], records)

    assert len(merged) == _common.MAX_ACTIONS
    assert merged[-1] == {"status": "evicted", "count": 11}, "60 in, 49 kept, 11 evicted"
    assert [row["call_key"] for row in merged[:-1]] == [
        f"web_fetch|{index:04d}" for index in range(11, 60)
    ], "the OLDEST are the ones dropped"


def test_merge_accumulates_the_eviction_count_across_writes():
    """One summary row, cumulative — the bound is a running property, not a per-write latch."""
    first = _common.merge_actions([], [_record(f"web_fetch|{index:04d}") for index in range(60)])
    second = _common.merge_actions(first, [_record("web_fetch|new")])

    assert len(second) == _common.MAX_ACTIONS
    assert second[-1] == {"status": "evicted", "count": 12}
    assert second[-2]["call_key"] == "web_fetch|new"


def test_merge_keeps_the_executed_record_over_a_later_replay():
    """A replay must never erase the fact that this run performed the effect."""
    executed = _record("web_fetch|abc", "executed")
    replayed = _record("web_fetch|abc", "replayed")

    assert _common.merge_actions([executed], [replayed]) == [executed]
    assert _common.merge_actions([replayed], [executed]) == [executed]
    assert len(_common.merge_actions([executed], [executed])) == 1, "one effect, one record"


def test_merge_drops_entries_that_cannot_be_replayed():
    """A keyless record cannot be replayed, so persisting it would claim otherwise."""
    junk = [{"tool": "web_fetch", "status": "executed"}, "nope", {"call_key": "", "status": "x"}]

    assert _common.merge_actions([], junk) == []


def test_prior_actions_reads_only_the_record_list():
    record = _record("web_fetch|abc")
    assert _common.prior_actions({"actions": [record, "junk"]}) == [record]
    assert _common.prior_actions({"actions": "nope"}) == []
    assert _common.prior_actions(None) == []
    assert _common.prior_actions({}) == []


# ─────────────────────────────────────────────────────────────────────────────
# 6. A2A8-02 — only a run that WROTE its outcome may announce it
# ─────────────────────────────────────────────────────────────────────────────
def test_a_lease_lost_run_pushes_no_owner_notification(queue_db, install_run_spy, execute_body,
                                                       steal_claim, monkeypatch):
    """The run refused to write the outcome; the notification leg must refuse to announce it."""
    from app.config import settings
    from app.services import notification_service

    pushed: list[tuple[int, str]] = []

    async def _record_push(db, task_id, outcome):
        pushed.append((task_id, outcome))

    monkeypatch.setattr(notification_service, "notify_owner_for_terminal_outcome", _record_push)
    monkeypatch.setattr(settings, "task_claim_lease_seconds", 3)
    task_id = queue_db.create_task(title="a3 lease lost")
    install_run_spy(sleep_s=6.5)  # the heartbeat floor is 5 s; the takeover lands at t≈1 s

    takeover = steal_claim(task_id, after_s=1.0, thief="thief-worker-1")
    outcome = execute_body(task_id)
    takeover.result(timeout=30)

    assert outcome["status"] == "failed"
    assert pushed == [], "a run that wrote no outcome must not tell the owner one happened"
    row = queue_db.row(task_id)
    assert row["claimed_by"] == "thief-worker-1" and row["result_summary"] is None
    lost = queue_db.query("SELECT * FROM activity_log WHERE action = 'task_lease_lost'")
    assert len(lost) == 1, "the loss is still recorded, naming the task"


def test_a_written_outcome_still_notifies_the_owner(queue_db, install_run_spy, execute_body,
                                                    monkeypatch):
    """Negative control: the guard must not silence the runs that DID write their outcome."""
    from app.services import notification_service

    pushed: list[tuple[int, str]] = []

    async def _record_push(db, task_id, outcome):
        pushed.append((task_id, outcome))

    monkeypatch.setattr(notification_service, "notify_owner_for_terminal_outcome", _record_push)
    task_id = queue_db.create_task(title="a3 completed run notifies")
    install_run_spy()

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed"
    assert pushed == [(task_id, "completed")]


# ─────────────────────────────────────────────────────────────────────────────
# 7. A2-03 — a refused hand-off re-arms, it never terminalises
# ─────────────────────────────────────────────────────────────────────────────
def test_stamp_rearmed_marks_the_rows_the_reconciler_reclaims(queue_db, monkeypatch):
    """The marker is the ONE contract with `maintenance._reconcile_stranded_pending`."""
    from celery_app.tasks import maintenance

    task_id = queue_db.create_task(title="a3 rearmed row")
    stale = (datetime.now(UTC) - timedelta(seconds=maintenance.PENDING_RECONCILE_AFTER_SECONDS + 60))

    async def _stamp():
        async with queue_db.factory() as db:
            stamped = await _common.stamp_rearmed(db, [task_id], reason="enqueue_refused:Test")
            await db.commit()
        return stamped

    assert _common.run_async(_stamp()) == [task_id]
    meta = json.loads(queue_db.row(task_id)["metadata"])
    assert meta["rearm_reason"] == "enqueue_refused:Test"
    assert meta["rearmed_at"]
    assert queue_db.row(task_id)["status"] == "pending", "re-arming never terminalises a row"

    # A window passes: the stamp is now older than the reconcile horizon.
    patched = dict(meta, rearmed_at=stale.isoformat())

    async def _backdate():
        async with queue_db.factory() as db:
            from sqlalchemy import select

            from app.models.task import Task

            row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            row.metadata_json = patched
            await db.commit()

    _common.run_async(_backdate())

    sent: list[int] = []
    import celery_app.tasks.agent_tasks as agent_tasks

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda tid: sent.append(int(tid)),
                        raising=False)
    outcome = _common.run_async(maintenance._reconcile_stranded_pending())

    assert sent == [task_id], "the reconciler re-dispatched the re-armed row"
    assert task_id in outcome["task_ids"]
    assert task_id in outcome["rearmed_ids"]


def test_stamp_rearmed_is_a_no_op_for_an_empty_list(queue_db):
    """A caller with nothing to stamp gets `[]` — no query, no marker, no exception."""

    async def _stamp():
        async with queue_db.factory() as db:
            return await _common.stamp_rearmed(db, [], reason="enqueue_refused:Nothing")

    assert _common.run_async(_stamp()) == []


def test_a_refused_hand_off_re_arms_the_rows_instead_of_terminalising_them(queue_db, monkeypatch):
    """A2-03: a group publishes message by message, so "nothing was delivered" is unknowable.

    The shipped code wrote `failed` on EVERY row when the publish raised — including rows whose
    message the broker had already accepted, and `claim_task` accepts only `pending`, so that work
    was queued AND written off.  After the fix the rows stay claimable and carry the reconciler's
    marker, so an undelivered row is re-dispatched instead of lost.
    """
    import celery

    ids = [queue_db.create_task(title="a3 hand-off row A"),
           queue_db.create_task(title="a3 hand-off row B")]

    class _RefusedGroup:
        def __init__(self, sigs) -> None:
            self.sigs = list(sigs)

        def apply_async(self):
            raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr(celery, "group", _RefusedGroup)

    with pytest.raises(PolsiaError) as excinfo:
        _common.run_async(_common.enqueue_created(ids, label="a3 sweep"))

    assert excinfo.value.code == "E_REDIS"
    assert excinfo.value.context["rearmed"] == ids
    assert excinfo.value.context["reason"] == "enqueue_refused:ConnectionError"

    rows = queue_db.query("SELECT id, status, error_message, metadata FROM tasks ORDER BY id")
    assert [row["status"] for row in rows] == ["pending", "pending"], (
        "a refused hand-off must leave every row claimable — delivered or not"
    )
    for row in rows:
        assert row["error_message"] is None, "the row is not written off, so it carries no failure"
        meta = json.loads(row["metadata"])
        assert meta["rearmed_at"], "each row is marked for the stranded-row reconciler"
        assert meta["rearm_reason"] == "enqueue_refused:ConnectionError"

    # A row whose message may already be live is claimable RIGHT NOW — the defect made it `failed`.
    queue_db.claim(ids[0], "a3-worker")
    assert queue_db.row(ids[0])["claimed_by"] == "a3-worker"


def test_a_healthy_hand_off_stamps_nothing(queue_db, monkeypatch):
    """Negative control: the re-arm path must not touch rows when the broker took them."""
    import celery_app.tasks.agent_tasks as agent_tasks

    sent: list[int] = []
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda tid: sent.append(int(tid)),
                        raising=False)
    task_id = queue_db.create_task(title="a3 healthy hand-off")

    _common.run_async(_common.enqueue_created([task_id], label="a3 sweep"))

    assert sent == [task_id]
    row = queue_db.row(task_id)
    assert row["status"] == "pending"
    assert "rearmed_at" not in (row["metadata"] or ""), "no marker on a delivered row"
