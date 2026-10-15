"""Phone activity tick — SMS/missed-call events into the EXISTING activity feed (V16 / P-03).

WHAT THIS SOLVES
    The phone routes answer the UI, but the ORCHESTRATOR and the live activity feed only see what
    is written to `activity_log` (and published on `polsia:activity`).  Without this tick the
    owner's phone is invisible to the agents.  Every ≤5 minutes this job reads the Phone Link
    store through `services.phone_link_service` and publishes the two events the amendment names:

        "SMS received: <who>"   (action `phone_sms_received`)
        "Missed call: <who>"    (action `phone_missed_call`)

    An OUTGOING text is not news and a completed call is not news — only those two shapes are
    events, and the store decides them (nothing here is synthesised).

IDEMPOTENCE — THE F1-CLASS LESSON, TAKEN SERIOUSLY
    Two independent guards:
      * a **durable watermark** per source table (`message`, `mms`, `call_history` — separate id
        spaces, so one counter would let a low MMS id look already-read), stored in a JSON file
        beside the key store and written temp-file + `os.replace` (the `secret_store` pattern);
      * a **published (kind,id) ledger** — the dedupe key the amendment names.  The service's read
        window deliberately RE-READS a bounded lookback past the cursor (so a row that synced out
        of order is not lost), which is exactly why the ledger, not the cursor, is the guard that
        makes a re-read a no-op.  Tick twice -> zero duplicates (pinned by
        `tests/unit/celery/test_e5_phone_activity.py`).

FAILURE POSTURE (a maintenance tick must not lie, crash-loop, or invent)
    * absent/half-mounted/drifted store -> a NAMED `store_unavailable` result carrying
      `E_PHONE_UNAVAILABLE`; zero rows, zero publishes, no watermark written;
    * unreadable state file -> `state_corrupt` (`E_STORAGE`): re-scanning from scratch would
      re-publish the whole store, so the tick refuses and names the fix instead of guessing;
    * the feed row goes through `log_activity`, the shipped single-publish path — so the durable
      `activity_log` row and the Redis nudge are one call, exactly like every other activity
      writer in this repository.

RESIDUAL, STATED (not hidden)
    The activity row and the ledger live in different stores (the app DB and a JSON file), so they
    cannot commit atomically.  Ordering is rows FIRST, ledger SECOND: a crash inside that window
    can duplicate one tick's events on the next run, but can never lose them.  A duplicate is
    visible in the feed and the next tick is clean; a loss would be invisible.  Moving the ledger
    into the database would close the window and needs a migration — out of this lane's file
    territory (E5 owns no model/migration), so it is recorded here as the follow-up, not claimed.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.errors import E_STORAGE, PolsiaError
from app.services import phone_link_service as phone
from app.services.activity_service import log_activity
from celery_app.tasks._common import run_async
from celery_app.worker import app

logger = logging.getLogger(__name__)

#: The registered task name (pinned by `tests/parity/test_contract_parity.py`'s reachability check
#: and by the beat entry in `celery_app/beat_schedule.py` — declared-but-never-loaded is the defect
#: class this repository has a gate for).
TASK_NAME = "celery_app.tasks.phone_activity.publish_phone_activity"
#: The durable state file: `backend/data/phone_activity_state.json` (beside the DeepSeek key store,
#: so it rides the same `/app/data` volume inside the containers).  Tests redirect it by patching
#: this module attribute.
STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "phone_activity_state.json"
STATE_VERSION = 1
#: The (kind,id) ledger is bounded: the newest N keys are kept, which is far wider than the
#: service's lookback window, so nothing that could be re-read falls out of the ledger first.
LEDGER_MAX = 500
ACTIVITY_AGENT = "system"
SMS_ACTION = "phone_sms_received"
MISSED_CALL_ACTION = "phone_missed_call"
#: A body carried into `detail` is a bounded snippet; a CODE-bearing body is not carried at all.
DETAIL_TEXT_MAX = 140


# ── durable state ────────────────────────────────────────────────────────────────────────────


def _load_state() -> tuple[phone.PhoneWatermark, set[str]]:
    """The watermark + ledger from disk.  Missing file = a first run (empty state).

    A CORRUPT file refuses (`E_STORAGE`): silently resetting to "publish everything" is the
    double-publish class this item exists to prevent, and silently resetting to "publish nothing"
    would freeze the feed.  The operator gets the choice, named in `how_to_fix`.
    """
    path = Path(STATE_PATH)
    if not path.exists():
        return phone.PhoneWatermark(), set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        watermark = phone.PhoneWatermark.from_dict(payload["watermark"])
        published = payload["published"]
        if not isinstance(published, list):
            raise ValueError("published is not a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PolsiaError(
            E_STORAGE,
            f"the phone activity state file is unreadable ({type(exc).__name__}), so the "
            "already-published event set cannot be checked",
            how_to_fix=(
                f"inspect or delete {path}: deleting it makes the next tick re-scan the whole "
                "store and re-publish every event it finds"
            ),
            context={"path": str(path)},
        ) from exc
    return watermark, {str(key) for key in published}


def _ledger_id(key: str) -> int:
    """The numeric id inside a ledger key ('mms:5233' -> 5233); unparseable keys sort oldest.

    Numeric, not string order: as strings 'mms:999' outranks 'mms:5233', so a plain
    `sorted(keys)[-N:]` kept low-numbered stragglers and dropped keys published seconds ago.
    """
    _kind, _, raw = key.rpartition(":")
    try:
        return int(raw)
    except ValueError:
        return -1


def _trim_ledger(published: set[str]) -> list[str]:
    """The newest `LEDGER_MAX // 3` keys OF EACH KIND — a per-kind budget, on purpose.

    A single global slice is wrong across id spaces: sorting whole keys puts the families in
    kind-string order ('call' < 'mms' < 'sms'), so a busy message backlog starves the call
    family entirely — measured live: a 500-key ledger kept {mms: 375, sms: 125} and ZERO call
    keys, so the newest missed calls were re-published on every tick (tick2 == tick3 == tick5
    == {published: 3}).  Per-kind budgets guarantee the invariant each id space needs:
    LEDGER_MAX // 3 >= ACTIVITY_LOOKBACK (pinned by test_the_ledger_budget_covers_every_re_readable_row).
    """
    by_kind: dict[str, set[str]] = {}
    for key in published:
        by_kind.setdefault(key.rpartition(":")[0], set()).add(key)
    per_kind = max(1, LEDGER_MAX // 3)
    return [key for keys in by_kind.values() for key in sorted(keys, key=_ledger_id)[-per_kind:]]


def _save_state(watermark: phone.PhoneWatermark, published: set[str]) -> None:
    """Persist the watermark + the newest `LEDGER_MAX` keys atomically (temp file + `os.replace`)."""
    path = Path(STATE_PATH)
    payload = {
        "version": STATE_VERSION,
        "watermark": watermark.as_dict(),
        "published": _trim_ledger(published),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix="phone-activity-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(raw, path)
        except BaseException:
            Path(raw).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"the phone activity state file could not be written ({type(exc).__name__}: {exc})",
            how_to_fix=f"check that {path.parent} exists and is writable, then let the next tick run",
            context={"path": str(path)},
        ) from exc


# ── event shaping ────────────────────────────────────────────────────────────────────────────


def _detail_for(row: dict[str, Any]) -> dict[str, Any]:
    """The row's own fields, bounded — and NEVER a verification code.

    The activity feed is rendered in the dashboard, so a 2FA body must not be copied into it: a
    code-bearing message records that its body was withheld instead of the body.
    """
    detail: dict[str, Any] = {"kind": row["kind"], "id": row["id"], "when": row["when"]}
    if row["kind"] == "call":
        return detail
    detail["direction"] = row["dir"]
    text = (row["text"] or "").strip()
    if text:
        if phone.CODE_RE.search(text):
            detail["redacted"] = "message body withheld (it looks like a verification code)"
        else:
            detail["text"] = text[:DETAIL_TEXT_MAX]
    return detail


def _event_for(row: dict[str, Any]) -> dict[str, Any] | None:
    """The event a store row means, or `None` when it is not news (the amendment's two shapes)."""
    if row["kind"] in {"sms", "mms"}:
        if row["dir"] != "in":
            return None
        return {
            "action": SMS_ACTION,
            "summary": f"SMS received: {row['who']}",
            "detail": _detail_for(row),
        }
    if row["kind"] == "call":
        if row["call_type"] not in phone.MISSED_CALL_TYPES:
            return None
        return {
            "action": MISSED_CALL_ACTION,
            "summary": f"Missed call: {row['who']}",
            "detail": _detail_for(row),
        }
    return None


def _advance(current: phone.PhoneWatermark, rows: list[dict[str, Any]]) -> phone.PhoneWatermark:
    """The new cursor: the newest id AMONG THE ROWS READ (not the store's, which could move on)."""
    ids = {"sms": current.sms, "mms": current.mms, "call": current.call}
    for row in rows:
        key = "call" if row["kind"] == "call" else str(row["kind"])
        ids[key] = max(ids[key], int(row["id"]))
    return phone.PhoneWatermark(**ids)


# ── the tick ─────────────────────────────────────────────────────────────────────────────────


async def _publish_phone_activity() -> dict[str, Any]:
    """One pass: read the store, publish the new events, advance the durable state."""
    try:
        watermark, published = _load_state()
    except PolsiaError as exc:
        logger.warning("phone activity: %s", exc.message)
        return {"status": "state_corrupt", "code": exc.code, "published": 0, "scanned": 0}

    try:
        rows = phone.activity_since(watermark)
    except PolsiaError as exc:
        # An absent mount is an expected state, not a worker fault: the named code rides the
        # result (the UI/login path surfaces the same code) and nothing is written.
        logger.warning("phone activity: store unavailable (%s: %s)", exc.code, exc.message)
        return {
            "status": "store_unavailable",
            "code": exc.code,
            "message": exc.message,
            "published": 0,
            "scanned": 0,
            "skipped": 0,
        }

    scanned = len(rows)
    events: list[tuple[str, dict[str, Any]]] = []
    skipped = 0
    # `activity_since` is newest-first; the feed reads chronologically, so publish oldest first.
    for row in reversed(rows):
        key = f"{row['kind']}:{row['id']}"
        if key in published:
            skipped += 1
            continue
        event = _event_for(row)
        if event is None:
            continue
        events.append((key, event))

    if events:
        from app.core.database import get_session_factory

        session_factory = get_session_factory()
        async with session_factory() as db:
            for _key, event in events:
                await log_activity(
                    db,
                    ACTIVITY_AGENT,
                    event["action"],
                    event["summary"],
                    detail=event["detail"],
                )
            await db.commit()

    new_watermark = _advance(watermark, rows)
    _save_state(new_watermark, published | {key for key, _event in events})

    result = {
        "status": "completed",
        "published": len(events),
        "scanned": scanned,
        "skipped": skipped,
        "watermark": new_watermark.as_dict(),
    }
    logger.info("phone activity: %s", result)
    return result


@app.task(name=TASK_NAME)
def publish_phone_activity() -> dict[str, Any]:
    """The beat entry point (every 5 min, `expires` 4 min): one pass over the phone store."""
    return run_async(_publish_phone_activity())
