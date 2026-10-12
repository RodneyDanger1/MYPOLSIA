"""Polsia orchestration — the DAG planner for the morning cycle.

Lifted from lane 05 (`research/05-runtime-100x/code/runtime/planner_dag.py`) and wired to the
frozen settings (`planner_max_nodes`, `claude_max_concurrency`) and to the spend gate
(`app.agents.accounting.authorize`).

What it replaces, in the original's own terms (lane 03 defects D-01/D-06 and lane 05 §4):

  * the orchestrator's `tasks[]` was discarded, so the documented "06:00 Beat job enqueues 5-8
    tasks" could not hold — the plan becomes typed nodes here;
  * there was no idempotency anywhere on `tasks`, so a re-run (retry, redeploy, manual trigger)
    DOUBLE-CREATES the day's work and, with SANDBOX_MODE off, double-posts/double-emails. Here
    every node carries a deterministic `idempotency_key`, `classify_new_work()` decides
    new/skip/retry/reclaim, and `skip_duplicate` SATISFIES dependents so a re-run finishes what
    is missing instead of duplicating what is done (8 nodes → 0 dispatched / 8 skipped_duplicate);
  * the budget was invisible until the invoice: the spend gate now runs BEFORE every dispatch and
    a node that does not fit is `deferred_budget` — never truncated, never half-run;
  * "did it fail?" had two buckets: every node now ends in one of seven explicit states, so a
    product manager can see *why* something did not run.

Deterministic by construction: node ids are content hashes, all sorts are total, `plan_hash`
covers the canonical node set, so the same inputs always produce the same plan (and an auditor
can prove which plan ran).
"""
from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from app.agents.accounting import BudgetPolicy, BudgetStop, authorize
from app.config import settings
from app.schemas.agent_outputs import ALL_AGENT_TYPES

# A cross-reference note kept in code so the two orders cannot silently diverge: prompts are
# assembled in context_assembler.LAYER_ORDER; plan scheduling has no ordering beyond the DAG.
LAYER_ORDER_NOTE = "prompt layers: app.agents.context_assembler.LAYER_ORDER"


class SideEffect(StrEnum):
    READ = "read"                                       # web fetch, inbox read, metrics read
    INTERNAL_WRITE = "internal_write"                   # DB rows, drafts, memories
    EXTERNAL_REVERSIBLE = "external_reversible"         # schedule a post, queue an email
    EXTERNAL_IRREVERSIBLE = "external_irreversible"     # post, send, charge, refund, PR merge


class NodeState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEFERRED_BUDGET = "deferred_budget"
    BLOCKED_DEPENDENCY = "blocked_dependency"
    SKIPPED_DUPLICATE = "skipped_duplicate"   # already done today (satisfies dependents)
    SKIPPED_FAILED = "skipped_failed"         # failed earlier, retries exhausted (fails dependents)
    BLOCKED_POLICY = "blocked_policy"


SATISFIED_STATES = {NodeState.SUCCEEDED, NodeState.SKIPPED_DUPLICATE}
FAILURE_STATES = {NodeState.FAILED, NodeState.BLOCKED_POLICY, NodeState.SKIPPED_FAILED}

TERMINAL_STATES = {
    NodeState.SUCCEEDED, NodeState.FAILED, NodeState.DEFERRED_BUDGET,
    NodeState.BLOCKED_DEPENDENCY, NodeState.SKIPPED_DUPLICATE, NodeState.SKIPPED_FAILED,
    NodeState.BLOCKED_POLICY,
}

#: Task types the dispatcher can run. `deployment` is documented but unregistered (X-01); a plan
#: may name it, the dispatcher will refuse it by name rather than silently dropping the node.
VALID_AGENTS = set(ALL_AGENT_TYPES)


def stable_id(*parts: str) -> str:
    h = hashlib.sha256("|".join(p.strip().lower() for p in parts).encode("utf-8"))
    return h.hexdigest()[:16]


@dataclass
class PlanNode:
    node_id: str
    title: str
    agent_type: str
    priority: int = 3                     # 1 = highest
    est_cost_usd: float = 0.0
    est_duration_s: float = 60.0
    depends_on: tuple[str, ...] = ()      # node_ids
    idempotency_key: str = ""
    side_effect: SideEffect = SideEffect.INTERNAL_WRITE
    payload: dict = field(default_factory=dict)   # task description / acceptance etc.
    max_retries: int = 1
    state: NodeState = NodeState.PENDING
    attempts: int = 0
    result_code: str = ""

    def to_row(self) -> dict:
        return {
            "node_id": self.node_id, "title": self.title, "agent_type": self.agent_type,
            "priority": self.priority, "est_cost_usd": self.est_cost_usd,
            "est_duration_s": self.est_duration_s, "depends_on": list(self.depends_on),
            "idempotency_key": self.idempotency_key, "side_effect": self.side_effect.value,
            "state": self.state.value, "attempts": self.attempts, "result_code": self.result_code,
        }


# ---------------------------------------------------------------------------
# Build + validate
# ---------------------------------------------------------------------------

class PlanError(ValueError):
    """A plan that cannot be dispatched safely (a dependency cycle)."""


@dataclass
class Plan:
    date: str
    nodes: dict[str, PlanNode]
    plan_hash: str = ""
    warnings: list[str] = field(default_factory=list)

    def add(self, node: PlanNode) -> None:
        self.nodes[node.node_id] = node

    @property
    def order(self) -> list[str]:
        return list(getattr(self, "_order", []))

    @property
    def depth(self) -> dict[str, int]:
        return dict(getattr(self, "_depth", {}))


def build_plan(
    date: str,
    spec_nodes: Iterable[dict],
    *,
    warnings_out: list[str] | None = None,
    max_nodes: int | None = None,
) -> Plan:
    """spec_nodes: dicts shaped like the orchestrator LLM output
        {title, agent_type, priority, estimate_usd, estimate_seconds?, depends_on[list of titles],
         side_effect?, description?, acceptance?}

    Title references resolve to node ids; unresolved references are dropped WITH a warning.
    A cycle raises `PlanError` — a cyclic plan can never be dispatched half-way. `max_nodes`
    (default `settings.planner_max_nodes`) caps the day: extras are dropped with a warning.
    """
    plan = Plan(date=date, nodes={})
    warn: list[str] = []
    limit = int(settings.planner_max_nodes if max_nodes is None else max_nodes)

    # 1. create nodes, deterministic ids
    title_to_id: dict[str, str] = {}
    specs = list(spec_nodes)
    for spec in specs:
        title = str(spec.get("title", "")).strip()
        agent = str(spec.get("agent_type", "")).strip().lower()
        if not title:
            warn.append("node without title dropped")
            continue
        if agent not in VALID_AGENTS:
            warn.append(f"node '{title}': unknown agent_type '{agent}' dropped")
            continue
        if len(plan.nodes) >= limit:
            warn.append(f"node '{title}': plan exceeds planner_max_nodes={limit}; dropped")
            continue
        node_id = stable_id(date, agent, title)
        if node_id in plan.nodes or title in title_to_id:
            warn.append(f"duplicate node '{title}' collapsed")
            continue
        key = str(spec.get("idempotency_key") or stable_id(agent, title, date))
        try:
            cost = max(0.0, float(spec.get("estimate_usd") or spec.get("est_cost_usd") or 0.0))
            dur = max(1.0, float(spec.get("estimate_seconds") or spec.get("est_duration_s") or 60.0))
        except (TypeError, ValueError):
            warn.append(f"node '{title}': non-numeric estimate coerced to defaults")
            cost, dur = 0.0, 60.0
        prio = spec.get("priority", 3)
        prio = int(prio) if isinstance(prio, int | float) or (isinstance(prio, str) and prio.isdigit()) else 3
        side_effect = SideEffect(str(spec.get("side_effect") or "internal_write"))
        plan.add(PlanNode(
            node_id=node_id, title=title, agent_type=agent,
            priority=max(1, min(5, prio)), est_cost_usd=cost, est_duration_s=dur,
            idempotency_key=key,
            side_effect=side_effect,
            payload={
                "description": str(spec.get("description") or ""),
                "acceptance": str(spec.get("acceptance") or ""),
                **{k: v for k, v in (spec.get("payload") or {}).items()},
            },
        ))
        title_to_id[title] = node_id

    # 2. resolve dependencies by title; drop unknown refs (flagged)
    for spec in specs:
        title = str(spec.get("title", "")).strip()
        nid = title_to_id.get(title)
        if not nid or nid not in plan.nodes:
            continue
        deps: list[str] = []
        for ref in spec.get("depends_on") or []:
            rid = title_to_id.get(str(ref).strip())
            if rid and rid != nid:
                deps.append(rid)
            else:
                warn.append(f"node '{title}': unresolved dependency '{ref}' dropped")
        plan.nodes[nid].depends_on = tuple(sorted(set(deps)))

    # 3. cycle detection + topological order (Kahn)
    order = _topo_order(plan.nodes)   # raises PlanError on a cycle
    # 4. layer assignment (longest-path depth)
    depth: dict[str, int] = {}
    for nid in order:
        node = plan.nodes[nid]
        depth[nid] = 1 + max((depth[d] for d in node.depends_on), default=-1)
    plan.warnings = warn
    if warnings_out is not None:
        warnings_out.extend(warn)
    plan.plan_hash = _plan_hash(plan)
    plan._order = order
    plan._depth = depth
    return plan


def _topo_order(nodes: dict[str, PlanNode]) -> list[str]:
    indeg = {nid: 0 for nid in nodes}
    children: dict[str, list[str]] = {nid: [] for nid in nodes}
    for nid, node in nodes.items():
        for d in node.depends_on:
            indeg[nid] += 1
            children[d].append(nid)
    ready = [n for n, k in indeg.items() if k == 0]
    heap = list(ready)
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        nid = heapq.heappop(heap)
        order.append(nid)
        for ch in sorted(children[nid]):
            indeg[ch] -= 1
            if indeg[ch] == 0:
                heapq.heappush(heap, ch)
    if len(order) != len(nodes):
        cyclic = sorted(set(nodes) - set(order))
        raise PlanError(f"dependency cycle among nodes: {cyclic}")
    return order


def _plan_hash(plan: Plan) -> str:
    canon = json.dumps(
        sorted((n.node_id, n.title, n.agent_type, n.priority, round(n.est_cost_usd, 6),
                round(n.est_duration_s, 3), tuple(sorted(n.depends_on)), n.idempotency_key,
                n.side_effect.value) for n in plan.nodes.values()),
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Critical path
# ---------------------------------------------------------------------------

def critical_path(plan: Plan, weight: str = "est_duration_s") -> tuple[list[str], float]:
    """Longest path through the DAG by the given weight. Returns (path node ids, total)."""
    order: list[str] = plan.order
    best: dict[str, float] = {}
    prev: dict[str, str | None] = {}
    for nid in order:
        node = plan.nodes[nid]
        w = float(getattr(node, weight))
        if node.depends_on:
            d = max(node.depends_on, key=lambda x: best.get(x, 0.0))
            best[nid], prev[nid] = best[d] + w, d
        else:
            best[nid], prev[nid] = w, None
    if not best:
        return [], 0.0
    last = max(best, key=lambda n: best[n])
    path, cur = [], last
    while cur is not None:
        path.append(cur)
        cur = prev[cur]
    return list(reversed(path)), best[last]


# ---------------------------------------------------------------------------
# Dedupe / idempotency
# ---------------------------------------------------------------------------

def classify_new_work(plan: Plan, existing: dict[str, dict]) -> dict[str, str]:
    """existing: idempotency_key -> {"status": <task status>, "attempts": n, "age_hours": h}.

    Returns node_id -> action in
      new | skip_duplicate | skip_dead | retry_eligible | reclaim_stale.

    Semantics used by the scheduler:
      skip_duplicate = the work for this key already SUCCEEDED today -> dependents are satisfied,
                       the node itself is not re-dispatched (this is what makes a same-day re-run
                       cheap and safe);
      skip_dead      = it failed and retries are exhausted -> dependents are treated as failed
                       (children become blocked_dependency, honestly).
    """
    decisions: dict[str, str] = {}
    for nid, node in plan.nodes.items():
        row = existing.get(node.idempotency_key)
        if not row:
            decisions[nid] = "new"
            continue
        status = str(row.get("status", "")).lower()
        attempts = int(row.get("attempts", 0) or 0)
        age = float(row.get("age_hours", 0.0) or 0.0)
        if status in ("completed", "succeeded"):
            decisions[nid] = "skip_duplicate"
        elif status == "in_progress" and age < 2.0:
            decisions[nid] = "skip_duplicate"        # a live run owns it
        elif status == "in_progress" and age >= 2.0:
            decisions[nid] = "reclaim_stale"          # crashed worker
        elif status == "failed" and attempts < node.max_retries + 1:
            decisions[nid] = "retry_eligible"
        elif status == "failed":
            decisions[nid] = "skip_dead"              # retries exhausted; evening report shows it
        else:
            decisions[nid] = "new"
    return decisions


def existing_from_results(plan: Plan, node_states: dict[str, str], *, attempts: dict[str, int] | None = None,
                          age_hours: float = 0.0) -> dict[str, dict]:
    """Turn a first run's terminal node states into the `existing` map a re-run classifies.

    This is the bridge the dispatcher uses after writing task rows: only work that reached a
    terminal, *recorded* state becomes "existing" — a node that was deferred for budget must be
    proposed again, so it is deliberately absent from the map.
    """
    attempts = attempts or {}
    out: dict[str, dict] = {}
    for nid, state in node_states.items():
        node = plan.nodes.get(nid)
        if node is None:
            continue
        if state in (NodeState.SUCCEEDED.value, NodeState.SKIPPED_DUPLICATE.value):
            out[node.idempotency_key] = {"status": "completed", "attempts": attempts.get(nid, 1),
                                         "age_hours": age_hours}
        elif state in (NodeState.FAILED.value, NodeState.SKIPPED_FAILED.value,
                       NodeState.BLOCKED_POLICY.value):
            out[node.idempotency_key] = {"status": "failed",
                                         "attempts": attempts.get(nid, node.max_retries + 1),
                                         "age_hours": age_hours}
    return out


# ---------------------------------------------------------------------------
# Scheduling: event-driven list scheduler + the pre-dispatch budget gate
# ---------------------------------------------------------------------------

@dataclass
class Wave:
    index: int
    start_s: float
    node_ids: tuple[str, ...]
    est_cost_usd: float
    est_duration_s: float          # wall time of the batch = max node duration


@dataclass
class ScheduleResult:
    waves: list[Wave]
    dispatched: list[str]
    deferred_budget: list[str]
    blocked_dependency: list[str]
    skipped_duplicate: list[str]
    est_wall_s: float
    est_cost_usd: float
    budget_usd: float
    remaining_budget_usd: float
    per_node_state: dict[str, str]
    per_node_timing: dict[str, tuple[float, float]] = field(default_factory=dict)
    makespan_s: float = 0.0
    critical_path: list[str] = field(default_factory=list)
    critical_path_s: float = 0.0
    deferred_reasons: dict[str, str] = field(default_factory=dict)
    gate_denials: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "dispatched": len(self.dispatched),
            "deferred_budget": len(self.deferred_budget),
            "blocked_dependency": len(self.blocked_dependency),
            "skipped_duplicate": len(self.skipped_duplicate),
        }


def schedule(
    plan: Plan,
    *,
    budget_usd: float | None = None,
    max_parallel: int | None = None,
    dedupe: dict[str, str] | None = None,
    budget_policy: BudgetPolicy | None = None,
    day_spent_usd: float = 0.0,
    agent_spent_usd: dict[str, float] | None = None,
) -> ScheduleResult:
    """Event-driven list scheduler (greedy, priority-then-cost-then-id).

    Why not depth-barrier waves: a node only needs its OWN dependencies finished; depth barriers
    serialize unrelated branches. The simulation runs a clock, completes nodes when their duration
    elapses, and refills free slots from the ready set. Properties (asserted in the lane tests):

      * a node dispatches only when every dependency is satisfied;
      * skipping a node whose work already succeeded today SATISFIES its dependents; skipping a
        dead node (retries exhausted) FAILS them;
      * the budget gate runs BEFORE every dispatch. With a `BudgetPolicy` the gate is
        `accounting.authorize` (min of day-remaining-minus-reserve, per-agent remaining and the
        per-run cap) and it charges estimates as it dispatches, so the whole day cannot overshoot;
        without one it is a simple remaining-budget comparison;
      * a node that does not fit is `deferred_budget` with the refusal reason attached — never
        truncated, never started partially;
      * makespan >= the weighted critical path; with no resource contention it equals it.
    """
    if budget_policy is None and budget_usd is None:
        budget_usd = float(settings.agent_daily_cost_cap_usd)
    if budget_policy is not None and budget_usd is None:
        budget_usd = budget_policy.day_limit_usd
    budget_usd = float(budget_usd or 0.0)
    parallel = int(settings.claude_max_concurrency if max_parallel is None else max_parallel)
    parallel = max(1, parallel)

    dedupe = dedupe or {}
    agent_spent = dict(agent_spent_usd or {})
    states: dict[str, NodeState] = {}
    skipped: list[str] = []
    dispatched: list[str] = []
    deferred: list[str] = []
    blocked: list[str] = []
    deferred_reasons: dict[str, str] = {}
    notes: list[str] = []
    timing: dict[str, tuple[float, float]] = {}
    batches: list[list[str]] = []
    batch_starts: list[float] = []
    remaining = budget_usd
    day_spent_so_far = float(day_spent_usd)
    denials = 0

    # 1. pre-mark dedupe decisions
    for nid, _node in plan.nodes.items():
        action = dedupe.get(nid)
        if action == "skip_duplicate":
            states[nid] = NodeState.SKIPPED_DUPLICATE
            skipped.append(nid)
        elif action == "skip_dead":
            states[nid] = NodeState.SKIPPED_FAILED
            skipped.append(nid)

    pending = [nid for nid in plan.nodes if nid not in states]
    running: list[tuple[float, str]] = []          # (end_time, node_id)
    now = 0.0
    EPS = 1e-9

    def _ready_or_terminal(nid: str) -> NodeState | None:
        """None => not decidable yet (a dependency is undecided or still RUNNING); otherwise the
        state the node should take now (READY, BLOCKED_DEPENDENCY or DEFERRED_BUDGET).

        Refinement over lane 05 (documented in the lane receipt): a node whose dependency was
        itself blocked propagates `blocked_dependency` rather than `deferred_budget`. Budget
        deferral must mean "there was no money", never "an ancestor failed" — the evening report
        reads these states to say *why* something did not run.
        """
        node = plan.nodes[nid]
        dep_states = [states.get(d) for d in node.depends_on]
        if any(s is None or s is NodeState.DISPATCHED for s in dep_states):
            return None                                    # dependency not finished yet
        if any(s in FAILURE_STATES for s in dep_states):
            return NodeState.BLOCKED_DEPENDENCY
        if any(s in (NodeState.BLOCKED_DEPENDENCY, NodeState.BLOCKED_POLICY) for s in dep_states):
            return NodeState.BLOCKED_DEPENDENCY            # an unsatisfiable ancestor
        if any(s is NodeState.DEFERRED_BUDGET for s in dep_states):
            return NodeState.DEFERRED_BUDGET               # no budget for the dependency either
        return NodeState.READY

    def _budget_gate(node: PlanNode) -> tuple[bool, str]:
        """True when the node may be dispatched now. Never raises: a denial is a state."""
        if budget_policy is None:
            if node.est_cost_usd > remaining + EPS:
                return False, (
                    f"est ${node.est_cost_usd:.4f} exceeds remaining ${remaining:.4f}"
                )
            return True, ""
        try:
            authorize(
                budget_policy,
                agent_type=node.agent_type,
                est_cost_usd=node.est_cost_usd,
                day_spent_usd=day_spent_so_far,
                agent_spent_usd=agent_spent.get(node.agent_type, 0.0),
            )
        except BudgetStop as exc:
            return False, f"{exc.code}: {exc.reason}"
        return True, ""

    while True:
        # 2. complete anything whose time has come
        done = [t for t in running if t[0] <= now + EPS]
        for end, nid in done:
            states[nid] = NodeState.SUCCEEDED
            running.remove((end, nid))
        # 3. classify pending
        ready: list[str] = []
        still_pending: list[str] = []
        for nid in pending:
            verdict = _ready_or_terminal(nid)
            if verdict is None:
                still_pending.append(nid)
            elif verdict is NodeState.READY:
                ready.append(nid)
            else:
                states[nid] = verdict
                (blocked if verdict is NodeState.BLOCKED_DEPENDENCY else deferred).append(nid)
                if verdict is NodeState.DEFERRED_BUDGET:
                    deferred_reasons[nid] = "dependency deferred or blocked"
        pending = still_pending
        # 4. greedy dispatch in (priority, cost, id) order, budget-gated
        ready.sort(key=lambda n: (plan.nodes[n].priority, plan.nodes[n].est_cost_usd, n))
        free = parallel - len(running)
        for nid in list(ready):
            if free <= 0:
                break
            node = plan.nodes[nid]
            if node.payload.get("policy_denied"):
                states[nid] = NodeState.BLOCKED_POLICY
                blocked.append(nid)
                continue
            allowed, reason = _budget_gate(node)
            if not allowed:
                denials += 1
                states[nid] = NodeState.DEFERRED_BUDGET
                deferred.append(nid)
                deferred_reasons[nid] = reason
                continue
            states[nid] = NodeState.DISPATCHED
            dispatched.append(nid)
            remaining -= node.est_cost_usd
            day_spent_so_far += node.est_cost_usd
            agent_spent[node.agent_type] = agent_spent.get(node.agent_type, 0.0) + node.est_cost_usd
            end = now + node.est_duration_s
            running.append((end, nid))
            timing[nid] = (now, end)
            if batches and abs(batch_starts[-1] - now) < EPS:
                batches[-1].append(nid)
            else:
                batches.append([nid])
                batch_starts.append(now)
            free -= 1
        # ready nodes that got no slot stay pending for the next refill
        for nid in ready:
            if states.get(nid) is None:
                still_pending.append(nid)
        pending = still_pending
        # 5. advance the clock or stop
        if not pending and not running:
            break
        if running:
            now = min(t for t, _ in running)
        elif pending:
            # no dispatch progress possible (budget exhausted everywhere) -> defer the remainder
            for nid in pending:
                states[nid] = NodeState.DEFERRED_BUDGET
                deferred.append(nid)
                deferred_reasons.setdefault(nid, "no budget remains for dispatch")
            pending = []
        else:
            break

    makespan = max((t[1] for t in timing.values()), default=0.0)
    waves = [
        Wave(i, batch_starts[i], tuple(sorted(batches[i])),
             sum(plan.nodes[n].est_cost_usd for n in batches[i]),
             max(plan.nodes[n].est_duration_s for n in batches[i]) if batches[i] else 0.0)
        for i in range(len(batches))
    ]
    est_cost = sum(plan.nodes[n].est_cost_usd for n in dispatched)
    path, path_s = critical_path(plan)
    return ScheduleResult(
        waves=waves, dispatched=dispatched, deferred_budget=deferred,
        blocked_dependency=blocked, skipped_duplicate=skipped,
        est_wall_s=makespan, est_cost_usd=est_cost, budget_usd=budget_usd,
        remaining_budget_usd=remaining,
        per_node_state={n: s.value for n, s in states.items()},
        per_node_timing=timing, makespan_s=makespan,
        critical_path=path, critical_path_s=path_s,
        deferred_reasons=deferred_reasons, gate_denials=denials, notes=notes,
    )


# ---------------------------------------------------------------------------
# Same-day re-run: the idempotency path the original did not have
# ---------------------------------------------------------------------------

def plan_rerun(
    plan: Plan,
    first_run: ScheduleResult,
    *,
    budget_usd: float | None = None,
    max_parallel: int | None = None,
    budget_policy: BudgetPolicy | None = None,
    day_spent_usd: float = 0.0,
    agent_spent_usd: dict[str, float] | None = None,
    age_hours: float = 0.0,
) -> tuple[dict[str, str], ScheduleResult]:
    """Re-schedule the SAME plan for the same day, given what the first run already did.

    Returns `(dedupe_decisions, schedule_result)`. On a clean day this dispatches **nothing**
    (every node `skipped_duplicate`) and costs **$0.00** — the measured contrast with the
    original flat-list design, which re-creates and re-runs every task.
    """
    existing = existing_from_results(plan, first_run.per_node_state, age_hours=age_hours)
    decisions = classify_new_work(plan, existing)
    result = schedule(
        plan, budget_usd=budget_usd, max_parallel=max_parallel, dedupe=decisions,
        budget_policy=budget_policy, day_spent_usd=day_spent_usd, agent_spent_usd=agent_spent_usd,
    )
    return decisions, result


def flat_list_rerun(spec_nodes: Iterable[dict], existing: dict[str, dict], *, date: str) -> dict:
    """What the ORIGINAL flat-list morning cycle would do on a re-run.

    The original created a task row for every item the orchestrator returned, every time, with no
    key to collide on (`daily_cycle.py:24-64`, lane 03 defect D-06) — so a re-run duplicates the
    whole day. This helper counts that outcome so the comparison in the receipt is a run, not a
    claim.
    """
    created = 0
    duplicated = 0
    for spec in spec_nodes:
        title = str(spec.get("title", "")).strip()
        agent = str(spec.get("agent_type", "")).strip().lower()
        if not title or agent not in VALID_AGENTS:
            continue
        created += 1
        key = str(spec.get("idempotency_key") or stable_id(agent, title, date))
        if key in existing:
            duplicated += 1
    return {"created": created, "duplicated": duplicated,
            "note": "the original had no idempotency key, so every re-run re-creates the day"}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@dataclass
class PlanRunSummary:
    """Per-state counts + money for one plan run (what the evening report reads)."""

    date: str
    plan_hash: str
    counts: dict[str, int]
    est_cost_usd: float
    makespan_s: float
    critical_path_s: float
    warnings: list[str] = field(default_factory=list)

    def to_row(self) -> dict:
        return {
            "date": self.date, "plan_hash": self.plan_hash,
            "planned": sum(self.counts.values()),
            **self.counts,
            "est_cost_usd": round(self.est_cost_usd, 6),
            "makespan_s": round(self.makespan_s, 3),
            "critical_path_s": round(self.critical_path_s, 3),
        }


def plan_summary(plan: Plan, result: ScheduleResult) -> PlanRunSummary:
    order = [
        NodeState.SUCCEEDED.value, NodeState.FAILED.value, NodeState.DEFERRED_BUDGET.value,
        NodeState.BLOCKED_DEPENDENCY.value, NodeState.SKIPPED_DUPLICATE.value,
        NodeState.SKIPPED_FAILED.value, NodeState.BLOCKED_POLICY.value,
    ]
    counts = {state: 0 for state in order}
    for state in result.per_node_state.values():
        counts[state] = counts.get(state, 0) + 1
    return PlanRunSummary(
        date=plan.date, plan_hash=plan.plan_hash, counts=counts,
        est_cost_usd=result.est_cost_usd, makespan_s=result.makespan_s,
        critical_path_s=result.critical_path_s, warnings=list(plan.warnings),
    )


# ---------------------------------------------------------------------------
# Serial comparison for the wall-time claim (same DAG, original execution model)
# ---------------------------------------------------------------------------

def simulate_serial_wall_time(plan: Plan, dispatched: Iterable[str], overhead_s: float = 0.0) -> float:
    """The original ran tasks one at a time (or as unrelated Celery tasks with no dependency
    order). Serial wall time = sum of durations; this is the baseline the wave scheduler is
    compared against."""
    return sum(plan.nodes[n].est_duration_s + overhead_s for n in dispatched)


def simulate_wave_wall_time(result: ScheduleResult, overhead_s: float = 0.0) -> float:
    return sum(w.est_duration_s + overhead_s for w in result.waves)


__all__ = [
    "FAILURE_STATES",
    "LAYER_ORDER_NOTE",
    "NodeState",
    "Plan",
    "PlanError",
    "PlanNode",
    "PlanRunSummary",
    "SATISFIED_STATES",
    "ScheduleResult",
    "SideEffect",
    "TERMINAL_STATES",
    "VALID_AGENTS",
    "Wave",
    "build_plan",
    "classify_new_work",
    "critical_path",
    "existing_from_results",
    "flat_list_rerun",
    "plan_rerun",
    "plan_summary",
    "schedule",
    "simulate_serial_wall_time",
    "simulate_wave_wall_time",
    "stable_id",
]
