"""Prospects / email campaigns.

`prospects.email` is UNIQUE in the schema, and the original `create_prospect` let
the `IntegrityError` escape as a 500.  A duplicate import is a *validation* answer
(`E_VALIDATION` with the address in the context), not an internal error — the
caller can act on it.  Reads are newest-first on `(created_at DESC, id DESC)`.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.outreach import EmailCampaign, EmailLog, Prospect
from app.services.query_helpers import db_refusal, fetch_scalars


async def list_prospects(
    db: AsyncSession,
    status: str | None = None,
    limit: int = 100,
) -> list[Prospect]:
    stmt = select(Prospect)
    if status is not None:
        stmt = stmt.where(Prospect.status == status)
    stmt = stmt.order_by(Prospect.created_at.desc(), Prospect.id.desc()).limit(limit)
    return await fetch_scalars(db, stmt, what="prospects")


async def create_prospect(db: AsyncSession, email: str, **values: Any) -> Prospect:
    """Insert a prospect; a duplicate address is a named refusal, not a 500."""
    address = (email or "").strip()
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        raise PolsiaError(
            E_VALIDATION,
            f"prospect email {email!r} is not a usable address",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"email": email},
        )
    prospect = Prospect(email=address, **values)
    db.add(prospect)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise PolsiaError(
            E_VALIDATION,
            f"prospect {address} already exists",
            how_to_fix="re-use the existing prospect row (prospects.email is unique) "
            "or update its status instead of re-inserting",
            context={"email": address},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - translated below
        raise db_refusal("prospect insert", exc) from exc
    return prospect


async def list_campaigns(db: AsyncSession, limit: int = 50) -> list[EmailCampaign]:
    return await fetch_scalars(
        db,
        select(EmailCampaign).order_by(EmailCampaign.id.desc()).limit(limit),
        what="email campaigns",
    )


async def list_email_logs(db: AsyncSession, limit: int = 100) -> list[EmailLog]:
    return await fetch_scalars(
        db,
        select(EmailLog).order_by(EmailLog.id.desc()).limit(limit),
        what="email logs",
    )


__all__ = ["create_prospect", "list_campaigns", "list_email_logs", "list_prospects"]
