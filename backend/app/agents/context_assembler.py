"""Polsia runtime — layered, token-budgeted context assembler.

Lifted from lane 05 (`research/05-runtime-100x/code/runtime/context_assembler.py`) and wired to
the frozen setting `settings.planner_context_token_budget`: `budgets_for_total()` distributes the
configured budget over the eight named layers, and `assemble()` renders them in the fixed order.

Why the order is fixed and load-bearing: the CLI caches the system prompt as a prefix, so any
byte change in layer *n* invalidates the cache from *n* onward. Layers 1-5 (identity, tool
doctrine, company, business state, yesterday) are therefore stable across runs and form the
cacheable prefix delivered via `--append-system-prompt-file` with
`__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` before the volatile part; memories / today's tasks / the
task itself ride in the user turn. Timestamps are allowed only in the volatile suffix — the
classic cache mistake ("breakpoint on content that changes every request") is structurally
impossible here.

Grounded in:
  * Anthropic prompt caching — cache order tools -> system -> messages, 4 breakpoints / 20-block
    lookback, 5-minute default TTL, 512-4096-token minimum cacheable prefix
    (https://docs.claude.com/en/docs/build-with-claude/prompt-caching);
  * Claude Code CLI `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` and `--append-system-prompt-file`
    (https://code.claude.com/docs/en/cli-reference);
  * "smallest set of high-signal tokens"; compaction / note-taking / sub-agents as the three
    long-horizon strategies (https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents).

Token measurement: no tokenizer is available for every model, so `measure_tokens()` is a
*documented estimator* (chars/4 with a word-count crosscheck). Budgets are set with a 15% safety
margin and a hard ceiling, so estimator error cannot overflow a model window.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------

def measure_tokens(text: str) -> int:
    """Deterministic estimator of token count for English-ish text.

    crosscheck: max(chars/4, words * 1.3) approximates BPE behaviour for mixed prose; JSON-heavy
    content runs ~3.5 chars/token, so chars/4 is the safer (lower) bound. Callers apply the
    safety margin; see the layer budgets.
    """
    if not text:
        return 0
    chars = len(text)
    words = len(re.findall(r"\S+", text))
    return int(max(chars / 4.0, words * 1.3))


@dataclass
class Layer:
    name: str
    budget_tokens: int
    text: str = ""
    pinned: bool = True          # pinned layers are never dropped entirely, only truncated
    volatile: bool = False       # volatile layers are excluded from the cacheable prefix
    entries: list[str] = field(default_factory=list)   # for list-type layers

    def body(self) -> str:
        if self.entries:
            return "\n".join(self.entries)
        return self.text

    def tokens(self) -> int:
        return measure_tokens(self.body())


# Layer order matters (first = most stable, cached longest). Order is FIXED.
LAYER_ORDER = (
    "identity",          # L0  who the agent is + non-negotiable rules (static, cached)
    "tool_doctrine",     # L1  how tools must be used; output-contract reminder (static, cached)
    "company",           # L2  company profile / mission / value prop (changes rarely)
    "business_state",    # L3  KPIs, goals, budget state (changes daily)
    "yesterday",         # L4  yesterday's summary + open loops (changes daily)
    "memories",          # L5  retrieved memories, ranked (changes per task)
    "today_tasks",       # L6  today's plan slice relevant to this agent (changes per run)
    "task_brief",        # L7  THE task (volatile; never cached)
)

#: Relative weights, summing to 23,300 tokens — the shape lane 05 designed. The absolute budget
#: comes from `settings.planner_context_token_budget` via `budgets_for_total()`.
DEFAULT_BUDGETS = {
    "identity": 1600,
    "tool_doctrine": 1200,
    "company": 2000,
    "business_state": 2500,
    "yesterday": 2000,
    "memories": 8000,
    "today_tasks": 3500,
    "task_brief": 2500,
}
SAFETY_MARGIN = 0.15   # applied on top of the sum of layer budgets
HARD_CEILING_TOKENS = 160_000  # refuse to emit a prompt estimated above this


def budgets_for_total(total_tokens: int, weights: dict[str, int] | None = None) -> dict[str, int]:
    """Scale the layer weights so their sum is the configured context budget.

    `settings.planner_context_token_budget` is the ONE knob an operator turns; the per-layer
    shape stays fixed so the stable prefix keeps its cache-friendly length.
    """
    weights = weights or DEFAULT_BUDGETS
    total_weight = sum(weights.values()) or 1
    scale = max(0.02, float(max(1_000, int(total_tokens))) / float(total_weight))
    return {name: max(64, int(round(weight * scale))) for name, weight in weights.items()}


class ContextOverflow(ValueError):
    pass


class SecretLeak(ValueError):
    pass


# ---------------------------------------------------------------------------
# Secret / forbidden-content screen. Runs on the ASSEMBLED prompt (final gate).
# ---------------------------------------------------------------------------

_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_pat", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("stripe_secret", re.compile(r"(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("oauth_refresh_file", re.compile(r'"refreshToken"\s*:')),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),  # conservative: flagged, never auto-dropped
)

# Things that must never be assembled into any agent prompt, even truncated.
NEVER_INCLUDE = (
    "raw .env contents",
    "API/OAuth tokens or refresh tokens",
    "contents of ~/.claude credentials or keychain data",
    "another agent's system prompt",
    "full raw customer email bodies beyond the task-local truncation",
    "database dumps or unfiltered table scans",
)


def screen_prompt(text: str) -> list[str]:
    """Return the list of forbidden-pattern names found. Caller MUST raise."""
    return [name for name, pat in _FORBIDDEN_PATTERNS if pat.search(text)]


# ---------------------------------------------------------------------------
# Memory retrieval: rank + recency blend, budget-aware, deterministic.
# ---------------------------------------------------------------------------

@dataclass
class MemoryItem:
    memory_id: str
    title: str
    content: str
    similarity: float            # 0..1 from the vector store (1 - distance) or FTS rank
    age_days: float
    tags: tuple[str, ...] = ()
    pinned: bool = False


def recency_weight(age_days: float, half_life_days: float = 14.0) -> float:
    return 0.5 ** (max(0.0, age_days) / half_life_days)


def rank_memories(
    items: Iterable[MemoryItem],
    *,
    query_terms: Iterable[str],
    k: int,
    w_sim: float = 0.6,
    w_rec: float = 0.3,
    w_tag: float = 0.1,
    budget_tokens: int | None = None,
) -> list[MemoryItem]:
    """score = w_sim*similarity + w_rec*recency + w_tag*tag_overlap.

    Pinned items always come first (stable order), then by score, then memory_id. An optional
    token budget trims the tail. Deterministic for identical inputs.
    """
    terms = {t.lower() for t in query_terms if t.strip()}
    scored: list[tuple[float, MemoryItem]] = []
    for it in items:
        tag_overlap = len(terms & {t.lower() for t in it.tags}) / max(1, len(terms)) if terms else 0.0
        score = w_sim * it.similarity + w_rec * recency_weight(it.age_days) + w_tag * tag_overlap
        scored.append((score, it))
    pinned = sorted((p for p in scored if p[1].pinned), key=lambda x: x[1].memory_id)
    rest = sorted((p for p in scored if not p[1].pinned),
                  key=lambda x: (-x[0], x[1].memory_id))
    ordered = [it for _, it in pinned] + [it for _, it in rest[:k]]
    if budget_tokens is None:
        return ordered
    out: list[MemoryItem] = []
    used = 0
    for it in ordered:
        cost = measure_tokens(f"- {it.title}: {it.content}")
        if used + cost > budget_tokens and out:
            break
        out.append(it)
        used += cost
    return out


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------

@dataclass
class AssembledContext:
    stable_prefix: str            # cacheable layers (identity -> yesterday)
    volatile_suffix: str          # memories + today_tasks + task_brief (per-run)
    full_prompt: str
    per_layer_tokens: dict[str, int]
    stable_prefix_hash: str
    dropped_layers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_tokens: int = 0


def _render_layer(layer: Layer) -> str:
    body = layer.body().strip()
    if not body:
        return ""
    return f"<{layer.name}>\n{body}\n</{layer.name}>"


def assemble(
    layers: dict[str, Layer],
    *,
    budgets: dict[str, int] | None = None,
    label: str = "",
) -> AssembledContext:
    """Build the prompt. Compression rule per layer, in order:

       1. if over budget: drop trailing entries (for list layers) until it fits;
       2. then hard-truncate with an explicit `[... truncated ...]` marker (never silent);
       3. pinned layers are never dropped entirely, only truncated;
       4. if the *sum* still exceeds HARD_CEILING: raise `ContextOverflow`.

    The assembled artefact is screened for secrets; a hit raises `SecretLeak` and aborts the run.
    """
    budgets = {**DEFAULT_BUDGETS, **(budgets or {})}
    warnings: list[str] = []
    dropped: list[str] = []
    per_layer_tokens: dict[str, int] = {}
    rendered: dict[str, str] = {}

    for name in LAYER_ORDER:
        layer = layers.get(name)
        if layer is None:
            rendered[name] = ""
            per_layer_tokens[name] = 0
            continue
        limit = budgets.get(name, layer.budget_tokens)
        body = layer.body().strip()
        # 1. entry-drop pass
        if layer.entries and measure_tokens(body) > limit:
            entries = list(layer.entries)
            while entries and measure_tokens("\n".join(entries)) > limit:
                entries.pop()
            if len(entries) < len(layer.entries):
                warnings.append(f"{name}: {len(layer.entries) - len(entries)} entries dropped for budget")
            layer = Layer(layer.name, layer.budget_tokens, entries=entries,
                          pinned=layer.pinned, volatile=layer.volatile)
            body = layer.body().strip()
        # 2. hard truncation with explicit marker
        if measure_tokens(body) > limit:
            char_budget = max(64, int(limit * 4 * 0.92))
            body = body[:char_budget] + "\n[... truncated to fit context budget ...]"
            warnings.append(f"{name}: hard-truncated to ~{limit} tokens")
            if not layer.pinned and layer.volatile:
                dropped.append(name)
        rendered[name] = f"<{name}>\n{body}\n</{name}>" if body else ""
        per_layer_tokens[name] = measure_tokens(body)

    stable_parts = [rendered[n] for n in LAYER_ORDER if not layers.get(n, Layer(n, 0)).volatile]
    volatile_parts = [rendered[n] for n in LAYER_ORDER if layers.get(n, Layer(n, 0)).volatile]
    stable_prefix = "\n\n".join(p for p in stable_parts if p)
    volatile_suffix = "\n\n".join(p for p in volatile_parts if p)

    total = measure_tokens(stable_prefix) + measure_tokens(volatile_suffix)
    if total > HARD_CEILING_TOKENS:
        raise ContextOverflow(
            f"assembled prompt ~{total} tokens exceeds hard ceiling {HARD_CEILING_TOKENS}"
        )
    full = (stable_prefix + "\n\n" + volatile_suffix).strip()

    hits = screen_prompt(full)
    if hits:
        raise SecretLeak(f"forbidden content in assembled prompt ({label}): {hits}")

    return AssembledContext(
        stable_prefix=stable_prefix,
        volatile_suffix=volatile_suffix,
        full_prompt=full,
        per_layer_tokens=per_layer_tokens,
        stable_prefix_hash=hashlib.sha256(stable_prefix.encode("utf-8")).hexdigest()[:16],
        dropped_layers=dropped,
        warnings=warnings,
        total_tokens=total,
    )


def split_for_cli(ctx: AssembledContext) -> dict[str, str]:
    """How the assembled context maps onto CLI flags:

      * stable prefix   -> `--append-system-prompt-file` (supports the
                           `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` split so the static part stays
                           cached);
      * volatile suffix -> the user prompt, passed via argv when it fits, else piped to stdin.
    """
    return {
        "append_system_prompt_file_content": ctx.stable_prefix,
        "user_prompt": ctx.volatile_suffix,
    }


def business_state_text(kpis: dict, goals: list[str] | None, budget: dict) -> str:
    """The L3 body (kept beside the budgets so the shape and its cost travel together)."""
    lines = ["KPIs:"]
    for k, v in sorted(kpis.items()):
        lines.append(f"- {k}: {v}")
    if goals:
        lines.append("Goals (in order):")
        lines += [f"{i + 1}. {g}" for i, g in enumerate(goals)]
    lines.append(
        "Budget: day_limit=${day:.2f} spent=${spent:.2f} remaining=${remaining:.2f}".format(
            day=budget.get("day_limit", 0.0),
            spent=budget.get("spent", 0.0),
            remaining=budget.get("remaining", 0.0),
        )
    )
    return "\n".join(lines)


__all__ = [
    "AssembledContext",
    "ContextOverflow",
    "DEFAULT_BUDGETS",
    "HARD_CEILING_TOKENS",
    "LAYER_ORDER",
    "Layer",
    "MemoryItem",
    "NEVER_INCLUDE",
    "SAFETY_MARGIN",
    "SecretLeak",
    "assemble",
    "budgets_for_total",
    "business_state_text",
    "measure_tokens",
    "rank_memories",
    "recency_weight",
    "screen_prompt",
    "split_for_cli",
]
