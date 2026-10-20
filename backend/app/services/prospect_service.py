"""Prospect CRM — the owner's create/read/update/delete over `prospects` (V27/L5).

Before this module the only writers of `prospects` were the outreach agent's worker
(`outreach_service.record_sent_emails`) and the public opt-out link
(`outreach_service.unsubscribe_prospect`); the OWNER could read the list and nothing else.
This is the missing owner surface, and it adds no side effects beyond the table: no email is
sent, no address is verified, no enrichment provider is called, nothing is scraped.

Rules this module is the ONE definition of:

* **Normalise before the unique insert.** `prospects.email` is UNIQUE. Every address is
  trimmed + lowercased BEFORE it is written, so `  Ada@Example.COM ` and `ada@example.com` are
  the same prospect — a duplicate that only collides after normalisation must not create a
  second row, and must not answer a 500 either.
* **Every refusal is named.** A blank/unusable address, a duplicate, an unknown id and a value
  the database's own CHECK rejects are all `PolsiaError`s with a code, a `how_to_fix` and a
  structured `context` (the offending field is `context["field"]`). Nothing here raises a bare
  exception out of a route.
* **The session survives a refused write.** A failed `flush()` poisons the SQLAlchemy session
  (every later statement raises `PendingRollbackError`), so the IntegrityError branches roll
  back before raising — the refusal is a normal answer, and the caller's session is usable
  immediately afterwards (this is why the duplicate tests can keep reading on the same session).
  The rollback is safe because a route performs exactly ONE write per request.
* **`status` is an open string.** The brief is explicit: existing rows carry free-text values
  (`new`, `contacted`, …) and this surface must not invent a strict enum that would reject them.
  The filter is therefore EXACT string equality, and the column's own CHECK — where a migrated
  database has one — is the last word, mapped to `E_VALIDATION` (`context.field = "status"`).
  A BLANK status is refused (`E_VALIDATION`, `context.field = "status"`); `null`/omitted means
  "unrecorded" (V29/L1 T1.3 — blank is caller error, not a second spelling of absent).
* **A short page is distinguishable from the whole table.** `list_prospects` returns
  `(rows, total)` where `total` is the count over the SAME predicates without LIMIT.

Reads are newest-first on `(created_at DESC, id DESC)` — the same order the outreach read uses,
so the two surfaces never disagree about "the latest prospect".
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, E_NOT_FOUND, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.outreach import EmailLog, Prospect
from app.services.query_helpers import fetch_scalar, fetch_scalars

logger = logging.getLogger(__name__)

#: The fields the owner may write (the PATCH surface — `schemas/prospect.ProspectUpdate`).
UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {"email", "first_name", "last_name", "company", "title", "source", "status"}
)

#: `prospects.email` is `String(255)`; a longer address cannot be stored, so it is refused as
#: caller input instead of reaching the driver.
MAX_EMAIL_LENGTH = 255

#: The column's own vocabulary — `ck_prospects_status` in `alembic/versions/0002_queue_and_perf.py`
#: (the same tuple `app.api.v1.status_filters.PROSPECT_STATUSES` serves the outreach read).
#: Used ONLY to describe a database CHECK refusal helpfully; it never refuses a value here
#: (the column is an open string — see the module docstring).
STATUS_VOCABULARY: tuple[str, ...] = (
    "new",
    "contacted",
    "replied",
    "converted",
    "unsubscribed",
    "bounced",
)


# ── named refusals ────────────────────────────────────────────────────────────────────────────


def _refuse(field: str, value: object, expected: str, message: str | None = None) -> PolsiaError:
    return PolsiaError(
        E_VALIDATION,
        message or f"prospect {field} {value!r} is invalid: {expected}",
        how_to_fix=ERROR_HINTS[E_VALIDATION],
        context={"field": field, "value": value, "expected": expected},
    )


def normalize_email(email: object) -> str:
    """The address as it will be STORED: trimmed and lowercased — or a named refusal.

    The structural checks are deliberately the same ones `outreach_service` applies to an
    address that arrives from the send path (one '@', neither side empty), plus the column's
    length.  They are not an RFC parser: a deliverable-looking address that this accepts is
    still unverified — nothing here claims otherwise, because nothing here verifies it.
    """
    if not isinstance(email, str):
        raise _refuse("email", email, "a string email address")
    address = email.strip().lower()
    if (
        not address
        or len(address) > MAX_EMAIL_LENGTH
        or address.count("@") != 1
        or address.startswith("@")
        or address.endswith("@")
        or any(char.isspace() for char in address)
    ):
        raise _refuse("email", email, "one '@' with text on both sides (trimmed + lowercased)")
    return address


def _text(value: object) -> str | None:
    """An optional column's value: trimmed, an empty/blank string becomes NULL, not `""`.

    `""` and NULL render differently in every list ("—" vs an empty line), and a blank name is
    not a recorded name — so the blank is stored as "not recorded", never as the empty string.
    `status` is the ONE exception: blank is caller error there (`_status_value`).
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _status_value(value: object) -> str:
    """A status the owner supplied, trimmed — or the named `E_VALIDATION` when it is BLANK.

    `null`/omitted already means "unrecorded" (the column default on create, a cleared value on
    update), so a blank string is not a second spelling of it: it is a form that sent nothing and
    would otherwise be stored as NULL, hiding the broken caller.  V29/L1 T1.3 — a deliberate
    semantic: `""`/whitespace is refused with `context.field = "status"`, `null` is unrecorded.
    """
    text = str(value).strip()
    if not text:
        raise PolsiaError(
            E_VALIDATION,
            "prospect status cannot be blank; omit it or pass null for 'unrecorded'",
            how_to_fix='omit status, pass null, or use a lifecycle word like "new"',
            context={"field": "status", "value": str(value)},
        )
    return text


def _integrity_refusal(address: str, exc: SQLAlchemyError) -> PolsiaError:
    """Translate a `prospects` IntegrityError by the constraint that actually fired.

    Order matters: a CHECK on `status` also mentions the table, so it is identified FIRST; the
    duplicate branch then matches the unique key on `email` (SQLite: "UNIQUE constraint failed:
    prospects.email"; PostgreSQL: `duplicate key value violates unique constraint
    "prospects_email_key"`).  Anything else is still a named `E_VALIDATION` — the row came from
    the owner's own body, so an unclassified constraint is caller input, never a 500 — and
    carries the driver's words in `context` so nothing is hidden.
    """
    raw = getattr(exc, "orig", None)
    detail = str(raw) if raw is not None else str(exc)
    lowered = detail.lower()
    if "ck_prospects_status" in lowered:
        return PolsiaError(
            E_VALIDATION,
            f"prospect status is not accepted by the database's ck_prospects_status check: "
            f"{detail}",
            how_to_fix=(
                "use one of the column's values: " + ", ".join(STATUS_VOCABULARY)
            ),
            context={
                "field": "status",
                "valid": list(STATUS_VOCABULARY),
                "constraint": "ck_prospects_status",
            },
        )
    if "email" in lowered and ("unique" in lowered or "duplicate" in lowered):
        return PolsiaError(
            E_VALIDATION,
            f"prospect {address} already exists",
            how_to_fix=(
                "re-use the existing prospect row (prospects.email is unique) or PATCH its "
                "fields instead of inserting a second one"
            ),
            context={"field": "email", "value": address, "constraint": "prospects.email"},
        )
    return PolsiaError(
        E_VALIDATION,
        f"the database refused the prospect write: {detail}",
        how_to_fix=ERROR_HINTS[E_VALIDATION],
        context={"field": "row", "value": address, "constraint": detail},
    )


# ── reads ────────────────────────────────────────────────────────────────────────────────────


def _search_conditions(search: str | None) -> list[Any]:
    """`WHERE` terms for the search box: email / first name / last name / company, contains.

    LIKE metacharacters in the operators' own input are ESCAPED (`%` and `_` are characters a
    company name can really carry — "100% Cotton"), so a search for `%` matches the rows that
    contain a percent sign instead of returning the whole table.
    """
    term = (search or "").strip()
    if not term:
        return []
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return [
        or_(
            Prospect.email.ilike(pattern, escape="\\"),
            Prospect.first_name.ilike(pattern, escape="\\"),
            Prospect.last_name.ilike(pattern, escape="\\"),
            Prospect.company.ilike(pattern, escape="\\"),
        )
    ]


def _conditions(search: str | None, status: str | None) -> list[Any]:
    conditions = _search_conditions(search)
    if status is not None:
        # EXACT equality: the column is an open string (module docstring), so this filters to
        # the stored word itself — it never folds case and never invents a vocabulary.
        conditions.append(Prospect.status == status)
    return conditions


async def list_prospects(
    db: AsyncSession,
    *,
    search: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> tuple[list[Prospect], int]:
    """One newest-first page plus the FULL match count over the same filters.

    `limit` is bounded here as well as on the wire (`ge=1, le=500`): a direct caller that never
    passes through FastAPI's `Query` cannot ask for an unbounded table read either.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise _refuse("limit", limit, "an integer 1-500")
    conditions = _conditions(search, status)
    stmt = select(Prospect)
    if conditions:
        stmt = stmt.where(*conditions)
    stmt = stmt.order_by(Prospect.created_at.desc(), Prospect.id.desc()).limit(limit)
    rows = await fetch_scalars(db, stmt, what="prospects")

    count_stmt = select(func.count()).select_from(Prospect)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = await fetch_scalar(db, count_stmt, what="prospects total")
    if total is None:  # `SELECT count(*)` always yields one row; no row is a broken driver
        raise PolsiaError(
            E_DB,
            "prospects total count query returned no row",
            how_to_fix=ERROR_HINTS[E_DB],
            context={"query": "prospects total"},
        )
    return list(rows), int(total)


async def get_prospect(db: AsyncSession, prospect_id: int) -> Prospect:
    """One row by id, or the named `E_NOT_FOUND` (the routes answer it 404)."""
    try:
        key = int(prospect_id)
    except (TypeError, ValueError) as exc:
        raise _refuse("prospect_id", prospect_id, "an integer id") from exc
    row = await db.get(Prospect, key)
    if row is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"prospect {key} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"prospect_id": key},
        )
    return row


# ── writes ───────────────────────────────────────────────────────────────────────────────────


async def create_prospect(
    db: AsyncSession,
    *,
    email: str,
    first_name: str | None = None,
    last_name: str | None = None,
    company: str | None = None,
    title: str | None = None,
    source: str | None = None,
    status: str | None = None,
) -> Prospect:
    """Insert one owner-created prospect (flushed; `.id` set); duplicates answer `E_VALIDATION`.

    `status=None` leaves the COLUMN default (`new`) in charge — the service never invents a
    lifecycle word for a row the owner did not give one to.  A BLANK status is refused by name
    (`_status_value`), never folded into the same "unrecorded" as an omitted one.
    """
    address = normalize_email(email)
    row = Prospect(
        email=address,
        first_name=_text(first_name),
        last_name=_text(last_name),
        company=_text(company),
        title=_text(title),
        source=_text(source),
    )
    if status is not None:
        row.status = _status_value(status)
    db.add(row)
    try:
        await db.flush()
    except SQLAlchemyError as exc:
        # The failed flush poisons the session: roll back so the refusal is an answer and the
        # caller can keep using this session (exactly one write per request — nothing to lose).
        await db.rollback()
        raise _integrity_refusal(address, exc) from exc
    return row


async def update_prospect(
    db: AsyncSession, prospect_id: int, changes: dict[str, Any]
) -> Prospect:
    """Apply ONLY the provided fields (the route passes `model_dump(exclude_unset=True)`).

    An unknown key is refused before the row is read; `email`, when present, is normalised and
    uniqueness-checked by the same unique constraint a create hits; an explicit `null` clears a
    nullable column; an empty string is stored as NULL (`_text`).  `status` is the exception: a
    blank one is refused by name (`_status_value`) — `null` clears it, `"   "` is caller error.
    """
    unknown = set(changes) - UPDATABLE_FIELDS
    if unknown:
        raise _refuse(
            "changes",
            sorted(unknown),
            f"only {sorted(UPDATABLE_FIELDS)} may be updated",
        )
    row = await get_prospect(db, prospect_id)
    address = row.email
    for field, value in changes.items():
        if field == "email":
            address = normalize_email(value)
            row.email = address
        elif field == "status" and value is not None:
            # The incoming status is validated BEFORE the row is touched, so a refused PATCH
            # leaves the stored word exactly as it was (an explicit null still clears it).
            row.status = _status_value(value)
        else:
            setattr(row, field, _text(value))
    try:
        await db.flush()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise _integrity_refusal(address, exc) from exc
    return row


async def delete_prospect(db: AsyncSession, prospect_id: int) -> Prospect:
    """Remove the row and return it (the route answers 204; the caller reports the id).

    A prospect the outreach agent has already emailed is the PARENT of its `email_logs` rows,
    and the database refuses the delete (`email_logs.prospect_id ON DELETE RESTRICT`, alembic
    `0002_queue_and_perf.py:306`).  That refusal is the named `E_VALIDATION` below — with the
    send-history count — never a bare `IntegrityError` out of a route (this module's promise).
    The failed flush poisons the session, so the rollback runs first, exactly like the create
    path's integrity branches; the caller's session is usable immediately afterwards.
    """
    row = await get_prospect(db, prospect_id)
    key = int(row.id)
    try:
        await db.delete(row)
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        sent = (
            await db.execute(
                select(func.count()).select_from(EmailLog).where(EmailLog.prospect_id == key)
            )
        ).scalar_one()
        count = int(sent or 0)
        if not count:
            # A DELETE on this table has exactly one documented refusal (the FK above); any other
            # integrity refusal is still named, but it must not borrow that explanation.
            raise PolsiaError(
                E_VALIDATION,
                f"the database refused the delete of prospect {key}: {str(exc.orig)[:200]}",
                how_to_fix="inspect the rows that reference this prospect and retry",
                context={"prospect_id": key, "constraint_detail": str(exc.orig)[:200]},
            ) from exc
        raise PolsiaError(
            E_VALIDATION,
            f"prospect {key} has {count} outreach email(s) recorded, so it cannot be deleted "
            "(prospects is the parent of email_logs and the database refuses the delete)",
            how_to_fix=(
                "keep the send history instead of deleting the prospect: mark it unsubscribed "
                'with PATCH /api/v1/prospects/{id} {"status": "unsubscribed"}'
            ),
            context={
                "prospect_id": key,
                "email_logs": count,
                "constraint": "email_logs.prospect_id",
            },
        ) from exc
    return row


__all__ = [
    "MAX_EMAIL_LENGTH",
    "STATUS_VOCABULARY",
    "UPDATABLE_FIELDS",
    "create_prospect",
    "delete_prospect",
    "get_prospect",
    "list_prospects",
    "normalize_email",
    "update_prospect",
]
