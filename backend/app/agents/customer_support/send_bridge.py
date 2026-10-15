"""The ONE send path for a customer-support reply (V15/4-01 — audit finding A1, #1 LOCKED).

WHY a module and not two lines in `agent.py`: CS replies and outreach mail share the SendGrid
path and the email channel's caps, but the CS agent called `integrations.send_email` DIRECTLY —
so every reply bypassed the whole compliance floor the outreach path built
(`email_compliance.send_compliant_email`: footer, AI-assistance disclosure, sender identity,
suppression).  That is a per-email legal exposure (CAN-SPAM, Appendix A4), and the fix is the
SAME shape as V11/W1's outreach bridge: ONE place, two fail-closed resolution modes, and
`integrations.send_email` unreachable from the CS package (pinned at source + AST level in
`tests/unit/test_v15_e2_cs_compliance.py`).

Mirrors `email_outreach/compliance_send.py` exactly (handoff H1, frozen in E2's receipt):

1. **Worker-resolved preflight** (`context[PREFLIGHT_KEY]` — deliberately THE SAME key outreach
   consumes, built by `outreach_service.compliance_preflight`): `{"company": <identity mapping>,
   "suppressed": [addresses]}` — the `channel_sends` pattern: the worker owns the DB and hands
   the agent what it needs.  No session, no loop, no I/O: the refusals are the same steps on the
   same data.  E3's enlistment flip (`agent_tasks._with_compliance_preflight`) turns this mode on
   for CS runs — one key, one producer.
2. **No preflight**: the guarded sender runs on a LOOP-LOCAL session this module opens and
   disposes.  A dedicated engine is required, not an optimisation: an `AsyncEngine` created in
   another event loop must not be reused from this one (asyncpg pins pooled connections to the
   loop that created them), and the process-wide engine may well have been created by the
   worker's loop.  The cost is one engine per send batch — named here rather than hidden, and it
   disappears entirely once mode 1 is wired.

Neither mode can reach `integrations.send_email` without a complete footer and a suppression
check, and every refusal propagates as the `PolsiaError` it is (E_CONFIG naming the missing
identity fields, E_POLICY_DENIED for an opt-out, E_DB when the database itself cannot be read) —
the agent turns that into a named `refusals[]` entry, never a silent send.
"""
from __future__ import annotations

import logging
from typing import Any, Final

from app.agents.base_agent import _run_coroutine_sync  # the repo's own sync->async bridge
from app.agents.email_outreach.compliance_send import (
    PREFLIGHT_KEY as _OUTREACH_PREFLIGHT_KEY,
)
from app.services.email_compliance import send_compliant_email
from app.services.query_helpers import db_refusal

logger = logging.getLogger(__name__)

#: The run-context key carrying the worker-resolved compliance preflight (mode 1).  THE SAME key
#: outreach reads on purpose (one preflight contract, one E3 enlistment flip, one key every
#: caller and test reads): `email_outreach.compliance_send.PREFLIGHT_KEY == "compliance"`.
PREFLIGHT_KEY: Final[str] = _OUTREACH_PREFLIGHT_KEY


def send_cs_reply(
    *,
    to: str,
    subject: str,
    body: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one support reply through the guarded sender; return its result verbatim.

    Raises the guarded sender's own `PolsiaError` refusals unchanged (a caller must not be able
    to mistake a refusal for a delivery), and wraps a non-`PolsiaError` database failure as the
    named `E_DB` refusal it is — never as a plausible-looking `{"ok": False}` the caller might
    retry blind.
    """
    preflight = (context or {}).get(PREFLIGHT_KEY)
    try:
        if isinstance(preflight, dict) and preflight:
            return _run_coroutine_sync(
                send_compliant_email(
                    None, to=to, subject=subject, body=body, preflight=preflight
                )
            )
        return _run_coroutine_sync(_send_on_own_session(to=to, subject=subject, body=body))
    except Exception as exc:  # noqa: BLE001 — translated below; PolsiaError re-raised as itself
        from app.core.errors import PolsiaError

        if isinstance(exc, PolsiaError):
            raise
        raise db_refusal("customer_support compliance check", exc) from exc


async def _send_on_own_session(*, to: str, subject: str, body: str) -> dict[str, Any]:
    """Mode 2: the guarded sender on a loop-local engine/session, disposed in `finally`."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings

    url = settings.database_url
    kwargs: dict[str, Any] = {"echo": settings.db_echo}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30.0}
    engine = create_async_engine(url, **kwargs)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            return await send_compliant_email(db, to=to, subject=subject, body=body)
    finally:
        await engine.dispose()


__all__ = ["PREFLIGHT_KEY", "send_cs_reply"]
