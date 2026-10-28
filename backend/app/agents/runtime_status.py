"""The NON-SECRET runtime status block every agent prompt carries (F2-FEED-1).

Live defect: the Live feed showed task 296 "Connectivity check - model key" marked completed by
`business_planning` while the run's own summary said it had "no visibility into whether a model key
is configured in dispatch".  The runtime KNOWS the provider, the model id, whether a model key is
configured and whether the sandbox is on — the prompt simply never carried them, so the run could
not answer its own task and the worker recorded `completed` anyway.

This module is the ONE producer of those facts and the ONE renderer of the prompt block:

  * `runtime_status()`             -> the facts, read from the SAME seams the dispatch uses
                                      (`deepseek_runtime.available()` decides the provider, the
                                      runtime's own `configured_model()` names the model);
  * `render_runtime_status_block()` -> the prompt text (nothing secret in it).

SECRET DISCIPLINE (pinned by `tests/unit/test_f2_feed1_runtime_status.py`): the block carries NO
key, NO key prefix and not even the store's masked display form.  `model_key_configured` is a
boolean and `model_key_source` is one of `stored|env|none|unknown`; the key store is read through
`secret_store.deepseek_key_status()` and only those two non-secret fields are ever copied out of
it.  A status read that fails reports `unknown` — never a fabricated `true`/`false`.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

#: The ONLY key-store locators the block may name (`secret_store.deepseek_key_status()["source"]`).
KEY_SOURCES: tuple[str, ...] = ("stored", "env", "none", "unknown")

#: What the DeepSeek seam is called in the block when it is not the runtime a dispatch would take.
CLI_PROVIDER = "claude_cli"


def _deepseek_key_source(configured: bool) -> str:
    """`stored` | `env` | `none` | `unknown` — never the key, never its masked display form."""
    if not configured:
        return "none"
    try:
        from app.services.secret_store import deepseek_key_status

        source = str(deepseek_key_status().get("source") or "").strip()
    except Exception as exc:  # noqa: BLE001 — a status read must never take a run down
        logger.warning(
            "code=runtime_key_status_unavailable: the model-key source is reported as unknown (%s)",
            type(exc).__name__,
        )
        return "unknown"
    return source if source in KEY_SOURCES else "unknown"


def cli_model_id() -> str:
    """The model the Claude CLI path would be asked for; honest "not pinned" when none is set."""
    pinned = str(os.getenv("POLSIA_CLAUDE_MODEL") or "").strip()
    return pinned or "the CLI's own default (not pinned by this deployment)"


def runtime_status(*, deepseek: bool, mock_mode: bool) -> dict[str, Any]:
    """The non-secret facts about the runtime that will RUN this agent.

    `deepseek` is the provider seam's OWN predicate (`deepseek_runtime.available()`), passed in by
    `base_agent.layers_for` so the block and the tool doctrine answer the same question — a second
    probe here could disagree with the runtime a run really takes.  `mock_mode` is the sanctioned
    `CLAUDE_CLI_MOCK` flag: a mocked run performs no model call, so a "connectivity check" in that
    mode must say so rather than report a live answer it never measured.
    """
    if deepseek:
        from app.agents import deepseek_runtime

        model = deepseek_runtime.configured_model()
    else:
        model = cli_model_id()
    return {
        "provider": "deepseek" if deepseek else CLI_PROVIDER,
        "model": model,
        "model_key_configured": bool(deepseek),
        "model_key_source": _deepseek_key_source(bool(deepseek)),
        "sandbox_mode": bool(getattr(settings, "sandbox_mode", True)),
        "mock_mode": bool(mock_mode),
    }


def render_runtime_status_block(status: dict[str, Any]) -> str:
    """The prompt text for one `runtime_status()` mapping — no secret, no invented fact."""
    provider = str(status.get("provider") or "unknown")
    model = str(status.get("model") or "unknown")
    configured = bool(status.get("model_key_configured"))
    source = str(status.get("model_key_source") or "unknown")
    if source not in KEY_SOURCES:
        source = "unknown"
    key_line = (
        f"- model_key_configured: true (a DeepSeek API key is present; source: {source})"
        if configured
        else "- model_key_configured: false (no DeepSeek key — runs use the Claude CLI's own login)"
    )
    sandbox = "true (external effects are refused/drafted)" if status.get("sandbox_mode") \
        else "false (external actions attempt the real path and refuse by name when unconfigured)"
    lines = [
        "RUNTIME STATUS (this worker's live, NON-SECRET configuration — the authoritative answer "
        "to \"is a model key configured?\", \"which model runs?\", \"is the sandbox on?\")",
        f"- provider: {provider}",
        f"- model: {model}",
        key_line,
        f"- sandbox_mode: {sandbox}",
        f"- mock_mode: {'true (CLAUDE_CLI_MOCK — no model call is made)' if status.get('mock_mode') else 'false (a real model call is made)'}",
        "- Quote these values verbatim when a task asks about this deployment's configuration. If "
        "a question cannot be answered from this block, the context, or your tools, say the fact "
        "is unknown in `notes` and set status=\"refused\" when the task's whole purpose is that "
        "check — never report a check as passed that you did not perform.",
    ]
    return "\n".join(lines)


__all__ = [
    "CLI_PROVIDER",
    "KEY_SOURCES",
    "cli_model_id",
    "render_runtime_status_block",
    "runtime_status",
]
