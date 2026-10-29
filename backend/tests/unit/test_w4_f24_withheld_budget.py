"""F-24 follow-up (wave 4) — the withholding count must survive the memories layer's budget pass.

Measured on the REBUILT app (live in-container probe, 2026-10-28,
`forge/raw/w4/f24-scope-live.raw.txt`): with 20 resolved memories (one of them a foreign company's),
the assembled prompt recorded `memories: 13 entries dropped for budget` and the dropped tail
included `- (1 stored memories from a different company were withheld from this brief)` — so the
model never learned that ANY memory had been withheld.  The SCOPE key survived only because it was
inserted FIRST; the withholding count was APPENDED last, i.e. exactly what the assembler's
entry-drop pass removes first.

Pinned here:

  * an over-budget memories layer still states the withholding count (and the SCOPE key);
  * a foreign memory's text never reaches the prompt in either case;
  * the budget pass really did drop entries (so this is not passing because nothing overflowed);
  * an IN-budget layer keeps the same two head lines — no behaviour change for the normal case.
"""
from __future__ import annotations

from app.agents.business_planning.agent import BusinessPlanningAgent

FOREIGN_MARKER = "FOREIGN-COMPANY-ONLY-FACT"
WITHHELD_TEXT = "stored memories from a different company were withheld"


def _view(index: int, *, scope: str = "unscoped", content: str = "") -> dict:
    """One resolver view, in the exact shape `resolve_agent_memories` returns."""
    return {
        "memory_id": index,
        "title": f"memory {index}",
        "content": content or (f"fact {index}: " + "candle " * 700),  # ~4.2k chars each
        "category": "agent_run",
        "source": "competitor_research",
        "similarity": 1.0 - index / 100.0,
        "age_days": float(index),
        "tags": ["competitor_research"],
        "pinned": False,
        "company_scope": scope,
        "company_name": "Cinder & Lye" if scope == "current" else "",
    }


def _assemble(views: list[dict]) -> object:
    return BusinessPlanningAgent().assemble_prompt(
        {"title": "candle product story", "description": "write the launch copy"},
        {"memories": views},
    )


def test_the_withholding_count_survives_an_over_budget_memories_layer() -> None:
    """RED at HEAD: the count was the last entry, so the budget pass dropped it first."""
    views = [_view(i) for i in range(19)]
    views.append(_view(999, scope="foreign", content=FOREIGN_MARKER + " " + "candle " * 700))

    assembled = _assemble(views)

    # The budget pass must really have run, or this test proves nothing.
    dropped = [w for w in assembled.warnings if "entries dropped for budget" in w]
    assert dropped, assembled.warnings
    assert assembled.volatile_suffix.count("] memory ") < 20, "some entries must have been dropped"

    # The two head lines the model MUST see.
    assert WITHHELD_TEXT in assembled.volatile_suffix, (
        "the withholding count was dropped by the budget pass: the model cannot know a "
        "memory was withheld"
    )
    assert "SCOPE: [CURRENT COMPANY]" in assembled.volatile_suffix
    # The withheld memory's own text is still absent.
    assert FOREIGN_MARKER not in assembled.volatile_suffix


def test_the_head_lines_are_unchanged_for_an_in_budget_layer() -> None:
    """No behaviour change in the normal case: SCOPE first, then the count, then entries."""
    views = [_view(1, scope="current"), _view(2), _view(3, scope="foreign", content=FOREIGN_MARKER)]

    assembled = _assemble(views)

    lines = [
        line for line in assembled.volatile_suffix.splitlines()
        if line.startswith("SCOPE:") or line.startswith("- (") or line.startswith("- [")
    ]
    assert lines[0].startswith("SCOPE: [CURRENT COMPANY]"), lines[:3]
    assert lines[1].startswith("- (1 stored memories from a different company were withheld"), lines[:3]
    assert "- [CURRENT COMPANY] memory 1" in "\n".join(lines[2:])
    assert FOREIGN_MARKER not in assembled.volatile_suffix
    assert assembled.warnings == [] or all("dropped" not in w for w in assembled.warnings)
