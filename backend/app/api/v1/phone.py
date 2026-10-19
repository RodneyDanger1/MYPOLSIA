"""`/api/v1/phone` — the owner's phone: the frozen H-P1 read surface plus the V17 policy-gated send.

THE CONTRACT (frozen before this module was written)
    GET /api/v1/phone/summary                        -> {store_status, unread_total, threads[],
                                                          missed_calls_today[]}
    GET /api/v1/phone/messages?limit&thread_id&q     -> {messages[{when,dir,who,text,thread_id}]}
    GET /api/v1/phone/calls?limit&missed_only        -> {calls[{when,type,who,num_masked,dur_s}]}
    GET /api/v1/phone/codes?within_hours             -> {codes[{when,from,code,snippet}]}
    GET /api/v1/phone/media?thread_id                -> {parts[{part_id, content_type, size_bytes,
                                                          when, thread_id}], truncated}
    GET /api/v1/phone/media/{part_id}                -> the attachment's BYTES (binary route)
    GET /api/v1/phone/export                         -> application/zip download
    POST /api/v1/phone/send                          -> {sent_at, to_masked, driver}
        body {"to": "<thread_id | E.164 digits>", "body": "1..640 chars"}; sandbox / unpaired /
        unavailable / validation refuse BY NAME (`services.phone_send_service`), and every attempt
        (sent or refused) writes one `activity_log` row with the recipient masked and no body text.
    All of them: key-auth (the router dependency), envelope `{"ok": true, "data": {...}}` on
    success and the named refusal shape (`{"ok": false, "error": {code, message, how_to_fix,
    context}}` + `X-Polsia-Error-Code`) on failure — both produced by the SHIPPED plumbing
    (`deps.refusal_guard` -> `main._refusal_headers`/`to_http_detail`), never hand-rolled here.

ABSENT STORE = 503 `E_PHONE_UNAVAILABLE`
    Every route raises it through `phone_link_service` (mount missing, half-mount, drifted schema,
    locked file).  There is no degraded mode: a phone surface that answers `200 []` because the
    mount is gone is indistinguishable from "no messages", and that lie is exactly what the
    503 + code is for.  The UI lane renders the honest state from this code.

THE CODES ROUTE IS SPECIAL (amendment P-01/P-02)
    It exists for AUTH flows, not for dashboards: key-only (no session/UI path), and every call
    writes ONE `activity_log` row naming the access (count, window) — never the code values, which
    would otherwise land in the activity feed the dashboard renders.  The audit row is written
    inside the same `refusal_guard` block, so a refusal writes nothing: a read that did not happen
    is not an access.

READS RUN OFF THE EVENT LOOP
    The store is read with the stdlib `sqlite3` driver (sync, read-only, 5 s lock timeout).  Every
    call is dispatched with `asyncio.to_thread`, so a slow/locked store cannot stall the API's
    event loop; the service layer stays a plain sync module that tests can call directly.

THE EXPORT (the V15 temp-file/FileResponse pattern, reused — `data.ExportFileResponse`)
    Members are exactly the three H-P1 names: `messages.json`, `calls.csv`, `contacts.json` — the
    same archive the Hermes-side bridge writes, minus the HTML viewer and media.  The ZIP is built
    and counted in a `mkstemp` scratch file BEFORE the response object exists, streamed from disk,
    and removed by the background task even when the client disconnects mid-download.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Response
from fastapi import Path as PathParam
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.v1.data import ExportFileResponse
from app.core.errors import E_STORAGE, E_VALIDATION, PolsiaError
from app.services import phone_link_service as phone
from app.services import phone_media_service as media
from app.services import phone_send_service as sends
from app.services.activity_service import log_activity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/phone", tags=["phone"], dependencies=[Depends(verify_api_key)])

#: The audit action the codes route writes.  Named `phone_codes_read` so an operator scanning
#: `activity_log` sees every 2FA read without reading the codes themselves.
CODES_AUDIT_ACTION = "phone_codes_read"
CODES_AUDIT_AGENT = "system"
EXPORT_TEMP_PREFIX = "polsia-phone-export-"
EXPORT_TEMP_SUFFIX = ".zip"


# ── reads ────────────────────────────────────────────────────────────────────────────────────


@router.get("/summary")
async def phone_summary() -> dict[str, Any]:
    """Counts, unread total, top threads and today's missed calls — the dashboard card's feed."""
    with refusal_guard():
        data = await asyncio.to_thread(phone.summary)
    return {"ok": True, "data": data}


@router.get("/messages")
async def phone_messages(
    limit: int = Query(50, ge=1, le=phone.MESSAGE_LIMIT_MAX),
    thread_id: int | None = Query(None, ge=1),
    q: str | None = Query(None, min_length=1, max_length=200),
) -> dict[str, Any]:
    """Newest-first SMS+MMS rows, optionally one thread (`thread_id`) and/or a text search (`q`).

    `q` is additive beyond H-P1's `limit`/`thread_id` pair: the amendment's P-02 names the search
    parameter and the phone page's search box consumes it.
    """
    with refusal_guard():
        rows = await asyncio.to_thread(phone.messages, limit=limit, thread_id=thread_id, q=q)
    return {"ok": True, "data": {"messages": rows}}


@router.get("/calls")
async def phone_calls(
    limit: int = Query(50, ge=1, le=phone.MESSAGE_LIMIT_MAX),
    missed_only: bool = Query(False),
) -> dict[str, Any]:
    """Call history; `missed_only=true` keeps the measured missed legend (3 and 5)."""
    with refusal_guard():
        rows = await asyncio.to_thread(phone.calls, limit=limit, missed_only=missed_only)
    return {"ok": True, "data": {"calls": rows}}


@router.get("/codes")
async def phone_codes(
    limit: int = Query(10, ge=1, le=50),
    within_hours: float | None = Query(None, gt=0, le=24 * 30),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Verification codes from incoming messages — key-only, and every call is audit-logged.

    The log row is written by `log_activity` (the shipped single-publish activity path), so the
    orchestrator's feed shows that a code was read, by whom-as-the-owner, and how many — with the
    code values deliberately absent from `summary`/`detail`.
    """
    with refusal_guard():
        rows = await asyncio.to_thread(phone.codes, limit=limit, within_hours=within_hours)
        await log_activity(
            db,
            CODES_AUDIT_AGENT,
            CODES_AUDIT_ACTION,
            summary=(
                f"Phone verification codes read: {len(rows)} row(s)"
                + (f" within {within_hours:g} h" if within_hours else " (no window)")
                + " — values are not recorded"
            ),
            detail={"count": len(rows), "within_hours": within_hours, "limit": limit},
        )
    return {"ok": True, "data": {"codes": rows}}


# ── media ────────────────────────────────────────────────────────────────────────────────────


@router.get("/media")
async def phone_media_list(
    thread_id: int = Query(..., ge=1),
    limit: int = Query(media.MEDIA_LIST_LIMIT, ge=1, le=media.MEDIA_LIST_LIMIT),
) -> dict[str, Any]:
    """One thread's newest attachments: `{parts[{part_id, content_type, size_bytes, when,
    thread_id}], truncated}` — cap 50, and `truncated` NAMES the cap instead of hiding it.

    `thread_id` is required: media is only meaningful in the context of who sent it, and a global
    "every attachment on the phone" listing is not a surface this product offers.  The join is the
    MEASURED one (`mms_part.message_id -> mms.message_id -> mms.thread_id`, 1844/1844 resolved on
    the real store) — see `services/phone_media_service`.

    `limit` may NARROW the window (L4 renders 24 thumbnails at a time) but never widen it past
    `MEDIA_LIST_LIMIT`: a higher value is a coded 422 from the Query bound, not a silent clamp.
    An unknown thread answers `parts: []` — the store was read and holds nothing; that is honest,
    and distinguishable from the 503 an absent store raises.
    """
    with refusal_guard():
        data = await asyncio.to_thread(media.media_parts, thread_id=thread_id, limit=limit)
    return {"ok": True, "data": data}


@router.get("/media/{part_id}")
async def phone_media_bytes(part_id: int = PathParam(..., ge=1)) -> Response:
    """One attachment's BYTES — 200 binary, or a named refusal with the product's envelope.

    The whole blob is verified (declared size == stored byte count, within the 25 MiB cap) BEFORE
    the response object exists, so a refusal is always a clean coded JSON answer and a 200 is
    always a complete, byte-exact download.  There is no StreamingResponse and no temp file here
    on purpose: the measured largest part is ~2.2 MiB, so a bounded in-memory read is smaller
    than the machinery a partial-body risk would buy.

    `Content-Type` comes from the part row (sanitized; fallback `application/octet-stream`) and
    `Content-Disposition` from `part_id` + a fixed extension map — never from the row's `name`.
    This route answers HEADERS, not an envelope: the `{"ok": ...}` shape exists on the JSON route
    only, exactly as the plan froze it.
    """
    with refusal_guard():
        part = await asyncio.to_thread(media.read_part, part_id)
    return Response(
        content=part.data,
        media_type=part.content_type,
        headers={"Content-Disposition": part.disposition()},
    )


# ── send ────────────────────────────────────────────────────────────────────────────────────


async def _audit_send_attempt(
    db: AsyncSession,
    payload: dict[str, Any],
    outcome: sends.SendResult | None,
    refusal: PolsiaError | None,
) -> None:
    """Write the ONE audit row per attempt — masked recipient, body LENGTH, never the body.

    An audit failure is logged and swallowed: the send already happened (or refused) and the
    caller's answer must not turn into a 500 because the feed is unavailable. The row itself never
    carries the message text or an unmasked number — `activity_log` is rendered in the dashboard.
    """
    to_masked = sends.mask_target(payload.get("to"))
    body = payload.get("body")
    detail: dict[str, Any] = {
        "target": to_masked,
        "to_masked": to_masked,
        "body_chars": len(body) if isinstance(body, str) else 0,
    }
    if refusal is not None:
        summary = f"Phone SMS send refused: {refusal.code}"
        detail["code"] = refusal.code
        level = "warning"
    else:
        assert outcome is not None
        summary = f"Phone SMS sent to {outcome.to_masked} via {outcome.driver}"
        detail.update(
            {"driver": outcome.driver, "sent_at": outcome.sent_at, "to_masked": outcome.to_masked}
        )
        level = "info"
    try:
        await log_activity(
            db, sends.AUDIT_AGENT, sends.AUDIT_ACTION, summary=summary, level=level, detail=detail
        )
        # COMMIT the row HERE, before the caller raises its refusal: `get_db` auto-commits on a
        # successful response but ROLLS BACK when the route raises, so a refusal's audit row would
        # otherwise be erased with the transaction.  Measured live (V17 drill): the sandbox 403
        # answered correctly while ZERO phone_send rows ever landed; two-session pin added.
        await db.commit()
    except Exception:  # noqa: BLE001 — the audit must never rewrite the outcome it records
        logger.exception("phone send audit row could not be written")


@router.post("/send")
async def phone_send(
    payload: Any = Body(None), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Send one SMS through the owner's paired Google Messages web session.

    THE BODY IS TYPED `Any` + `Body(None)` ON PURPOSE (V-12).  Every validation mistake must answer
    the app's own named `E_VALIDATION` envelope, carry `X-Polsia-Error-Code`, AND leave one audit
    row — and a `dict`/`Body(...)` annotation defeats all three: FastAPI then rejects a non-object
    body (`[]`, `"x"`, `42`, `null`) — and an ABSENT body — BEFORE this function runs, with the
    framework's uncoded `{"detail": [...]}` 422 and ZERO audit rows, so the attempt is invisible in
    `activity_log`.  Measured at HEAD in `tests/unit/api/test_v18_phone_media.py` (RED leg).

    DEVIATION FROM THE PLAN'S LITERAL SIGNATURE, recorded in the lane RECEIPT: the plan wrote
    `payload: Any = Body(...)`; `Body(None)` is that same parameter made OPTIONAL so a `null` or
    absent body reaches the route instead of the framework handler.  The frozen RESPONSE contract
    is unchanged (`E_VALIDATION` 422 + envelope + header); the only behavioural delta is that those
    two input classes are now audited like every other attempt instead of vanishing.

    Ordering after the shape check is the service's: sandbox -> validation -> driver present ->
    subprocess. Refusals are raised as CODED `HTTPException`s (`sends.http_refusal`) so
    `E_PHONE_SEND_UNPAIRED` keeps its 409 without a `core/errors.py` edit; `main.py`'s coded handler
    supplies the envelope and header.
    """
    with refusal_guard():
        outcome: sends.SendResult | None = None
        refusal: PolsiaError | None = None
        body: dict[str, Any] | None = payload if isinstance(payload, dict) else None
        if body is None:
            received = (
                "null" if payload is None else "array" if isinstance(payload, list)
                else type(payload).__name__
            )
            refusal = PolsiaError(
                E_VALIDATION,
                f"the request body must be a JSON OBJECT with `to` and `body` (received JSON {received})",
                how_to_fix='send {"to": "<thread_id | E.164 digits>", "body": "1..640 chars"}',
                context={"received_type": received},
            )
        else:
            try:
                outcome = await sends.send(body.get("to"), body.get("body"))
            except PolsiaError as exc:
                refusal = exc
        # One row per attempt, ALWAYS — a non-object body has no target, so `mask_target(None)`
        # yields "" and the row still names the code (V-12).
        await _audit_send_attempt(db, body or {}, outcome, refusal)
        if refusal is not None:
            raise sends.http_refusal(refusal) from refusal
        assert outcome is not None
    return {"ok": True, "data": outcome.payload()}


# ── export ───────────────────────────────────────────────────────────────────────────────────


def _build_export() -> tuple[Path, dict[str, int]]:
    """Write `messages.json` + `calls.csv` + `contacts.json` into a scratch ZIP; return its path.

    Read ONCE, then write: the declared message count in the JSON header is the length of the list
    that is actually archived, so the file cannot disagree with itself.
    """
    messages = phone.all_messages()
    calls = phone.export_calls()
    contacts = phone.contacts()

    payload = json.dumps(
        {
            "exported": datetime.now().isoformat(timespec="seconds"),
            "count": len(messages),
            "messages": messages,
        },
        ensure_ascii=False,
        indent=1,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["when", "type", "who", "number", "duration_s"])
    for call in calls:
        writer.writerow([call["when"], call["type"], call["who"], call["number"], call["dur_s"]])

    try:
        fd, raw_path = tempfile.mkstemp(prefix=EXPORT_TEMP_PREFIX, suffix=EXPORT_TEMP_SUFFIX)
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"the phone export ZIP temp file could not be created in the system scratch dir "
            f"({type(exc).__name__}: {exc})",
            how_to_fix=f"check that {tempfile.gettempdir()} exists and is writable, then retry",
            context={"scratch_dir": tempfile.gettempdir()},
        ) from exc
    path = Path(raw_path)
    os.close(fd)  # Windows: release the mkstemp handle so ZipFile can reopen the path
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("messages.json", payload)
            archive.writestr("calls.csv", buffer.getvalue())
            archive.writestr("contacts.json", json.dumps(contacts, ensure_ascii=False, indent=1))
    except BaseException:
        _remove_export_tempfile(path)
        raise
    return path, {"messages": len(messages), "calls": len(calls), "contacts": len(contacts)}


def _remove_export_tempfile(path: Path) -> None:
    """Best-effort cleanup (idempotent, never raises) — the background task and every failure path."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - scratch-dir unlink is the only caller
        logger.warning("could not remove the phone export temp file %s (%s)", path, exc)


@router.get("/export")
async def phone_export() -> ExportFileResponse:
    """The archive download — temp file built and counted before the first byte (H-P1)."""
    with refusal_guard():
        path, counts = await asyncio.to_thread(_build_export)
    filename = f"polsia-phone-export-{date.today().isoformat()}.zip"
    return ExportFileResponse(
        path,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Polsia-Export-Messages": str(counts["messages"]),
            "X-Polsia-Export-Calls": str(counts["calls"]),
        },
        background=BackgroundTask(_remove_export_tempfile, path),
    )
