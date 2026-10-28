"""F-22 — what the BROKER still owes vs what the DB says, in one deterministic recovery.

The live incident (2026-10-28, this stack): the worker prefetched two `run_agent_task` messages,
lost Redis mid-prefetch (`TimeoutError`), and stayed alive-but-wedged with `unacked=2`.  A restart
did **not** restore them.  That is not a Celery bug, it is the documented shape of the Redis
transport:

* an unacked message lives in the `unacked` HASH plus its delivery instant in the `unacked_index`
  ZSET (`kombu/transport/redis.py`, `QoS.append`);
* it is restored only by a LIVE worker's event loop — `loop.call_repeatedly(10,
  cycle.maybe_restore_messages)` → `QoS.restore_visible` — and only once `visibility_timeout`
  (1800 s in this deployment, `celery_app/celery_config.py`) has elapsed since the delivery;
  `restore_visible` is additionally throttled to one real pass per 10 polls (`interval=10`);
* a `SIGKILL`ed worker restores nothing at shutdown (the virtual QoS's `restore_unacked_once` runs
  on a graceful close only).

So the message is not lost — it is invisible for up to `visibility_timeout` and only if some worker
is polling.  With Beat OFF (this wave's deliberate posture) the row-side recovery is missing too:
a row whose claim predates the delivery sits `in_progress` until `requeue_stale_claims` runs, and
that is a Beat tick.  Live specimen: task 315 `in_progress`, heartbeat frozen at the claim, its
delivery still in `unacked`; two further `unacked_index` tags had no payload at all (a manual
`HDEL` left them behind) — the "ghost" shape.

This module is the decision table, kept separate from the Celery task so it can be driven with a
broker double in tests (message queue doubles are sanctioned test boundaries).  The rules:

=====================  ===========================================================  ==============
DB truth for the row   Why                                                          Action
=====================  ===========================================================  ==============
missing                the row was deleted (data decision); nobody owes a run        DROP
terminal               completed/failed/cancelled/rejected — no work is owed; a     DROP
                       redelivery would be refused at claim and only make noise
pending, due           the delivery is owed; put it back in its queue               RESTORE
pending, future-dated  the cadence owns this row when it is due (`claim_task`      DROP
                       refuses a future `scheduled_date`, so restoring now would
                       burn the delivery for nothing)
in_progress, expired   the claim's last sign of life is older than the lease, so     RESTORE
claim                  the redelivered message can take the claim over atomically
in_progress, live      a worker that may still be alive owns it; the transport's     LEAVE
claim                   visibility timeout is the correct owner of that case
=====================  ===========================================================  ==============

Anything this module cannot reason about (a non-`run_agent_task` delivery, an undecodable body, a
delivery whose row cannot be read) is LEFT UNTOUCHED and named in the result — never guessed at,
never silently dropped, because dropping a message is an irreversible act.

Nothing here dispatches work: the only spend path is the restored message itself, which then goes
through the same claim, pause gate and budget gate every other delivery takes.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

#: The one task whose deliveries carry a `tasks.id` this recovery can check against the DB.
AGENT_TASK_NAME = "celery_app.tasks.agent_tasks.run_agent_task"

#: A delivery outcome.  `RESTORE` and `DROP` mutate the broker; the rest are observations.
RESTORED = "restored"
DROPPED = "dropped"
LEFT_LIVE_CLAIM = "left_live_claim"
LEFT_UNMANAGED = "left_unmanaged"
LEFT_UNDECODABLE = "left_undecodable"
LEFT_MISSING_ROW = "left_missing_row"  # reserved: a missing row is dropped, see decide_action
ERRORED = "errored"


@dataclass(frozen=True)
class Delivery:
    """ONE entry of the Redis transport's unacked state, decoded as far as it can be."""

    tag: str
    score: float | None
    task_name: str | None
    task_id: int | None
    has_payload: bool

    @property
    def managed(self) -> bool:
        """True when this recovery knows what the delivery is (an agent-task message)."""
        return self.has_payload and self.task_name == AGENT_TASK_NAME and self.task_id is not None


@dataclass
class RecoveryResult:
    """The named outcome of ONE pass.  Counts are measured; ids/tags are named, never invented."""

    status: str = "completed"
    reason: str | None = None
    scanned: int = 0
    bound: int = 0
    reaped_scanned: int = 0
    reaped_requeued: int = 0
    restored: list[int] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    left_live_claim: list[int] = field(default_factory=list)
    left_unmanaged: list[str] = field(default_factory=list)
    left_undecodable: int = 0
    errored: list[dict[str, str]] = field(default_factory=list)
    bound_reached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "scanned": self.scanned,
            "bound": self.bound,
            "bound_reached": self.bound_reached,
            "reaped_scanned": self.reaped_scanned,
            "reaped_requeued": self.reaped_requeued,
            "restored": list(self.restored),
            "dropped": list(self.dropped),
            "left_live_claim": list(self.left_live_claim),
            "left_unmanaged": list(self.left_unmanaged),
            "left_undecodable": self.left_undecodable,
            "errored": list(self.errored),
        }

    @property
    def changed(self) -> bool:
        """True when this pass mutated the broker (the only reason to write an activity row)."""
        return bool(self.restored or self.dropped)


class DeliveryBroker(Protocol):
    """The two reads + two mutations this recovery needs from a broker (doubles allowed in tests)."""

    def snapshot(self) -> list[Delivery]: ...

    def restore(self, tag: str) -> None: ...

    def drop(self, tag: str) -> None: ...

    def close(self) -> None: ...


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def decode_delivery(tag: str, payload: Any, score: float | None = None) -> Delivery:
    """Decode ONE `unacked` HASH value into a `Delivery` — never raising on a malformed body.

    The transport stores `[message._raw, exchange, routing_key]` (kombu `QoS.append`); the raw
    message's `body` is base64 of the Celery protocol-2 body `[[args...], kwargs, embed]`.  A
    payload that cannot be read is returned as `has_payload=False` with no task name/id, so the
    caller leaves the delivery alone instead of guessing.
    """
    if payload is None:
        return Delivery(tag=tag, score=score, task_name=None, task_id=None, has_payload=False)
    try:
        raw = json.loads(_as_text(payload))
        message = raw[0] if isinstance(raw, list) and raw else None
        if not isinstance(message, dict):
            return Delivery(tag=tag, score=score, task_name=None, task_id=None, has_payload=False)
        headers = message.get("headers")
        task_name = headers.get("task") if isinstance(headers, dict) else None
        if not isinstance(task_name, str) or not task_name:
            task_name = None
    except (ValueError, TypeError, KeyError, IndexError):
        return Delivery(tag=tag, score=score, task_name=None, task_id=None, has_payload=True)
    try:
        body = message.get("body")
        if not isinstance(body, str) or not body:
            return Delivery(tag=tag, score=score, task_name=task_name, task_id=None,
                            has_payload=False)
        decoded = json.loads(base64.b64decode(body))
        if isinstance(decoded, dict):  # protocol 1
            args = decoded.get("args")
        elif isinstance(decoded, list) and decoded:  # protocol 2: [args, kwargs, embed]
            args = decoded[0]
        else:
            args = None
        task_id: int | None = None
        if isinstance(args, list) and args:
            first = args[0]
            # bool is an int subclass; a `True` task id is not a task id.
            if isinstance(first, int) and not isinstance(first, bool):
                task_id = first
    except (ValueError, TypeError, KeyError, IndexError):
        # The message exists but its body is unreadable: KEEP the task name (it is a fact) and
        # leave the delivery for the caller to leave alone — never guess a row from garbage.
        return Delivery(tag=tag, score=score, task_name=task_name, task_id=None, has_payload=True)
    return Delivery(tag=tag, score=score, task_name=task_name, task_id=task_id, has_payload=True)


class KombuRedisDeliveryBroker:
    """The production adapter: kombu's OWN Redis transport state and restore primitives.

    Deliberately not a hand-rolled re-implementation of the restore transaction: `QoS.restore_by_tag`
    is the transport's own atomic `HGET`/`HDEL`+`ZREM`+`LPUSH`, and `QoS.ack` is its own
    `HDEL`+`ZREM` (dropping a delivery without requeueing it).  Using them keeps this recovery
    byte-compatible with what a live worker's visibility-timeout restore would do.
    """

    def __init__(self, connection: Any | None = None) -> None:
        from celery_app.worker import app

        self._connection = connection or app.connection_for_write()
        self._connection.ensure_connection(max_retries=1, timeout=2.0)
        self._channel = self._connection.channel()

    def snapshot(self) -> list[Delivery]:
        channel = self._channel
        with channel.conn_or_acquire() as client:
            unacked = client.hgetall(channel.unacked_key)
            index = client.zrange(channel.unacked_index_key, 0, -1, withscores=True)
        payloads: dict[str, Any] = {}
        for key, value in (unacked or {}).items():
            payloads[_as_text(key)] = value
        scores: dict[str, float] = {}
        for entry in index or []:
            if isinstance(entry, list | tuple) and len(entry) == 2:
                scores[_as_text(entry[0])] = float(entry[1])
        tags = sorted(set(payloads) | set(scores), key=lambda tag: scores.get(tag, 0.0))
        return [
            decode_delivery(tag, payloads.get(tag), scores.get(tag))
            for tag in tags
        ]

    def restore(self, tag: str) -> None:
        # The transport's own transaction: HDEL unacked + ZREM index + LPUSH the queue(s).
        self._channel.qos.restore_by_tag(tag)

    def drop(self, tag: str) -> None:
        # `QoS.ack` = the transport's own HDEL + ZREM, and its `_dirty` set tolerates a tag this
        # process never delivered (a previous process's delivery).
        self._channel.qos.ack(tag)

    def close(self) -> None:
        try:
            self._channel.close()
        finally:
            self._connection.release()
