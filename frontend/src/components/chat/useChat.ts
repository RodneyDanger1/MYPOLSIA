"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";

/**
 * The chat rail's data layer, against the FROZEN chat contract (plan §7 C1):
 *
 *   GET  /api/v1/chat?limit=50 → {items:[{role,content,created_at,task_id}], total}
 *   POST /api/v1/chat          {message} → 201 {reply:{text}, intent:{kind, extras}, task_id,
 *                              requires_confirm, confirm_token, refusal|null}
 *   POST /api/v1/chat/confirm  {confirm_token} → 200 {task_id} | 409 (a real HTTP failure)
 *
 * Honest-state notes, recorded because they are visible in the behaviour:
 *  - `total` stays null until a successful read answers — a page count is a claim, and there is
 *    no claim to make before the server declared one (never a fabricated 0).
 *  - A reply is appended to the transcript only after the 201 lands, and a malformed success
 *    body is refused by name (`E_CONTRACT`) rather than rendered as an invented bubble.
 *  - A failed send keeps the typed text (the rail only clears the composer on success) and its
 *    named failure is rendered from `sendError`; the message is not added to the transcript,
 *    because nothing proves the server persisted it.
 *  - The confirm gate is separate state. F-40 (wave 6): it can now be RESTORED from the stored
 *    proposal — `GET /api/v1/chat` carries `pending_confirm`, the still-open single-use token for
 *    the newest stored `create_task` proposal (the server re-issues it from the owner row and its
 *    ORIGINAL 15-minute window, and sends `null` once that window has closed or the token was
 *    consumed). A reload therefore brings the card back while the token is valid, instead of
 *    leaving a stored message that instructs the owner to click a control that is gone. A read
 *    never overwrites a gate this session already holds (a live or confirmed gate is newer).
 */

/** One persisted transcript row — the frozen GET shape. `role` is an open string on the wire
 *  (`'owner' | 'polsia'` today): the rail renders an unknown role with its own word instead of
 *  treating it as impossible. `created_at` is nullable like every timestamp in this app and
 *  renders as the shared "date unknown" placeholder. */
export type ChatMessageRow = {
  role: string;
  content: string;
  created_at: string | null;
  task_id: number | null;
};

/** The refusal envelope of the frozen 201 body. */
export type ChatRefusal = {
  code: string;
  message: string;
  how_to_fix?: string;
};

/** The frozen 201 body of POST /api/v1/chat. `intent.kind` is an open string carrying the three
 *  known branches (answer | create_task | refused); per-branch extras ride the same object.
 *
 *  F-29 (wave 4): a `create_task` reply also carries `paid_run` / `run_cost_cap_usd` /
 *  `day_cost_cap_usd` — the disclosure the confirm card must show BEFORE the click. They are
 *  typed as optional/nullable because a server that predates F-29 omits them; the card then says
 *  the caps were not declared instead of inventing numbers. */
export type ChatIntent = {
  kind: string;
  agent_type?: string | null;
  paid_run?: boolean | null;
  run_cost_cap_usd?: number | null;
  day_cost_cap_usd?: number | null;
  [key: string]: unknown;
};

export type ChatReply = {
  reply: { text: string };
  intent: ChatIntent;
  task_id: number | null;
  requires_confirm: boolean;
  confirm_token: string | null;
  refusal: ChatRefusal | null;
};

/**
 * F-29 — what confirming the gate actually starts, as the SERVER declared it on the 201 reply.
 *
 * `paidRun` is `null` when an older server sent no `paid_run` flag (the card then says it was not
 * declared, while still stating that a run is dispatched — that part is the frozen confirm
 * contract, not a server claim). The caps are the enforced per-run / daily ceilings from
 * `accounting.budget_policy_from_settings`; absent = not declared, never defaulted here.
 */
export type ChatDispatchDisclosure = {
  paidRun: boolean | null;
  runCostCapUsd: number | null;
  dayCostCapUsd: number | null;
};

/** The 200 body of POST /api/v1/chat/confirm. */
export type ChatConfirmAck = { task_id: number };

/**
 * F-40 — the stored proposal's still-open confirm gate, as `GET /api/v1/chat` restores it.
 *
 * The server sends this ONLY while the original single-use token's 15-minute window is open (and
 * `null` after expiry, consumption, or when no proposal is pending), so a caller can render a card
 * whenever this block exists. `intent` carries the same `create_task` facts the 201 served — the
 * paid-run caps included — so the restored card's disclosure is read, never re-guessed.
 */
export type ChatPendingConfirm = {
  chatMessageId: number | null;
  token: string;
  expiresAt: string | null;
  intent: ChatIntent;
};

/** Defensive parse of the additive `pending_confirm` block: only a token-bearing `create_task`
 *  gate survives. Anything else (an older server, a junk shape, an empty token) is "no gate" —
 *  never a card built on invented fields. */
function asPendingConfirm(value: unknown): ChatPendingConfirm | null {
  if (value === null || typeof value !== "object") return null;
  const bag = value as Record<string, unknown>;
  const token = typeof bag.token === "string" && bag.token.trim() !== "" ? bag.token : null;
  if (token === null) return null;
  const rawIntent = bag.intent;
  if (rawIntent === null || typeof rawIntent !== "object") return null;
  const intent = rawIntent as ChatIntent;
  if (intent.kind !== "create_task") return null;
  const chatMessageId =
    typeof bag.chat_message_id === "number" && Number.isInteger(bag.chat_message_id)
      ? bag.chat_message_id
      : null;
  return {
    chatMessageId,
    token,
    expiresAt: typeof bag.expires_at === "string" && bag.expires_at !== "" ? bag.expires_at : null,
    intent,
  };
}

/** A row as this rail holds it: the wire row plus the live-only extras the 201 body carries.
 *  A history read does NOT carry intent/refusal/token (frozen contract), so after a reload a
 *  reply renders as its persisted text only — the extras exist while the session's own send
 *  made them, which is exactly what the wire supports.
 *
 *  `key` is the row's STABLE identity (F-38): a wire row's fingerprint, or `local:<n>` from a
 *  monotonic counter for a row this session appended optimistically. `local` marks the latter
 *  until a history read proves the server holds it. */
export type ChatItem = ChatMessageRow & {
  refusal?: ChatRefusal | null;
  key: string;
  local?: true;
};

/** What identifies a row's CONTENT across the two sources: a local row's `created_at` is this
 *  browser's clock and the persisted row's is the server's, so timestamps can never be compared —
 *  role + text is the pair a history read can confirm. */
function confirmationKey(row: { role: string; content: string }): string {
  return `${row.role}\u0000${row.content}`;
}

/**
 * F-38 — merge a history read into the transcript instead of replacing it.
 *
 * The measured defect: `load()` ended with `setItems(data.items)`, so a read that STARTED before a
 * send and LANDED after it erased the exchange the user had just watched appear. Reads are now a
 * merge:
 *  - wire rows are the base, oldest-first as served, each with a fingerprint key (duplicate rows of
 *    the same role/text get an occurrence suffix so two identical messages stay two rows);
 *  - a `local` row survives while no read has confirmed its content, and is dropped (in favour of
 *    the server's own row) the moment one has — matched as a MULTISET, so a send repeated twice
 *    needs two wire rows before both local copies retire.
 *
 * Nothing local is ever overwritten by a read that does not contain it, and no row is invented:
 * the merged list is exactly the wire rows plus the rows this session is still waiting to see.
 */
export function mergeHistory(rows: ChatMessageRow[], prev: ChatItem[]): ChatItem[] {
  const occurrences = new Map<string, number>();
  const next: ChatItem[] = rows.map((row) => {
    const base = `wire:${confirmationKey(row)}\u0000${row.created_at ?? ""}`;
    const seen = (occurrences.get(base) ?? 0) + 1;
    occurrences.set(base, seen);
    return { ...row, key: seen === 1 ? base : `${base}\u0000#${seen}` };
  });

  const available = new Map<string, number>();
  for (const row of next) {
    const key = confirmationKey(row);
    available.set(key, (available.get(key) ?? 0) + 1);
  }

  const pending: ChatItem[] = [];
  for (const item of prev) {
    if (!item.local) continue;
    const key = confirmationKey(item);
    const remaining = available.get(key) ?? 0;
    if (remaining > 0) {
      available.set(key, remaining - 1); // this read confirms the local row — the wire row wins
      continue;
    }
    pending.push(item);
  }

  return [...next, ...pending];
}

/** The single confirm gate this rail can offer. Confirm tokens are single-use and the 201 reply is
 *  the only body that mints one, so at most the latest confirmable reply is actionable; an earlier
 *  unused token stays valid server-side until it is used or expires.
 *
 *  F-29: `disclosure` travels WITH the gate — the cost facts the server declared on the same 201
 *  that minted this token. It is never re-read or guessed at click time.
 *
 *  F-40: `restored` marks a gate rebuilt from the STORED proposal by a history read (never one this
 *  session minted) and `expiresAt` is that token's original expiry when the server declared one, so
 *  the card can state what it is rather than looking identical to a freshly minted gate. */
export type ChatConfirmGate = {
  token: string;
  status: "pending" | "confirming" | "confirmed" | "failed";
  taskId: number | null;
  error: string | null;
  disclosure: ChatDispatchDisclosure;
  restored: boolean;
  expiresAt: string | null;
};

/** A non-negative finite number off the wire, or `null` (never a coerced string/NaN). */
function asCost(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

/** F-29: the disclosure a create_task reply declared (see `ChatDispatchDisclosure`). */
function disclosureFrom(intent: ChatIntent | null | undefined): ChatDispatchDisclosure {
  const bag: Record<string, unknown> =
    intent !== null && typeof intent === "object" ? (intent as Record<string, unknown>) : {};
  return {
    paidRun: typeof bag.paid_run === "boolean" ? bag.paid_run : null,
    runCostCapUsd: asCost(bag.run_cost_cap_usd),
    dayCostCapUsd: asCost(bag.day_cost_cap_usd),
  };
}

function errorDisplay(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

/** The frozen history read (the contract's limit=50). */
const HISTORY_PATH = "/chat?limit=50";

export function useChat() {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ChatConfirmGate | null>(null);
  const readSeq = useRef(0);
  /** F-38: the local row sequence. Monotonic for the lifetime of the hook instance. */
  const localSeq = useRef(0);

  const load = useCallback(async ({ silent = false }: { silent?: boolean } = {}) => {
    const seq = ++readSeq.current;
    if (!silent) {
      setStatus("loading");
      setError(null);
    }
    try {
      const data = await api.get<{
        items: ChatMessageRow[];
        total: number;
        pending_confirm?: unknown;
      }>(HISTORY_PATH);
      if (seq !== readSeq.current) return; // a newer read already landed — drop this one
      // F-38: MERGE, never replace. A read that started before a send arrives with a body that
      // predates it; replacing the list erased the exchange the user had just watched appear.
      setItems((prev) => mergeHistory(Array.isArray(data?.items) ? data.items : [], prev));
      setTotal(typeof data?.total === "number" ? data.total : null);
      // F-40: restore the stored proposal's gate — but NEVER over one this session holds. A live
      // send's gate (pending/confirming/confirmed/failed) is newer than any read, and the server
      // stops sending the block once the token is consumed.
      const restored = asPendingConfirm(data?.pending_confirm);
      if (restored !== null) {
        setConfirm(
          (prev) =>
            prev ?? {
              token: restored.token,
              status: "pending",
              taskId: null,
              error: null,
              disclosure: disclosureFrom(restored.intent),
              restored: true,
              expiresAt: restored.expiresAt,
            },
        );
      }
      setStatus("ready");
      setError(null);
    } catch (e) {
      if (seq !== readSeq.current) return;
      setError(errorDisplay(e));
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const send = useCallback(async (text: string): Promise<ChatReply> => {
    setSending(true);
    setSendError(null);
    try {
      const reply = await api.post<ChatReply>("/chat", { message: text });
      // Refuse a malformed success body BY NAME rather than rendering an invented bubble.
      if (typeof reply?.reply?.text !== "string") {
        throw new ApiError(
          0,
          "the chat reply did not carry reply.text — nothing was appended",
          "E_CONTRACT",
          "",
        );
      }
      const now = new Date().toISOString();
      // F-38: a monotonic local sequence, never reused and never renumbered by a later read, so
      // these two rows keep one identity for as long as they are on screen.
      const owner: ChatItem = {
        role: "owner",
        content: text,
        created_at: now,
        task_id: null,
        key: `local:${++localSeq.current}`,
        local: true,
      };
      const replyRow: ChatItem = {
        role: "polsia",
        content: reply.reply.text,
        created_at: now,
        task_id: reply.task_id ?? null,
        refusal: reply.refusal ?? null,
        key: `local:${++localSeq.current}`,
        local: true,
      };
      setItems((prev) => [...prev, owner, replyRow]);
      // `total` stays the server's LAST declared count — never incremented by a local guess. The
      // two rows above are labelled as unconfirmed by `unconfirmed` until a read returns them.
      if (reply.requires_confirm && reply.confirm_token) {
        setConfirm({
          token: reply.confirm_token,
          status: "pending",
          taskId: null,
          error: null,
          // F-29: captured from THIS reply — the same body that minted the token. The card shows
          // it before the click; the click itself sends only the token.
          disclosure: disclosureFrom(reply.intent),
          // F-40: this gate is LIVE (the reply that minted it is on screen), not restored.
          restored: false,
          expiresAt: null,
        });
      }
      return reply;
    } catch (e) {
      setSendError(errorDisplay(e));
      throw e; // the rail keeps the typed text for another try
    } finally {
      setSending(false);
    }
  }, []);

  const confirmAction = useCallback(
    async (token: string) => {
      setConfirm((prev) =>
        prev && prev.token === token ? { ...prev, status: "confirming", error: null } : prev,
      );
      try {
        const ack = await api.post<ChatConfirmAck>("/chat/confirm", { confirm_token: token });
        setConfirm((prev) =>
          prev && prev.token === token
            ? { ...prev, status: "confirmed", taskId: ack?.task_id ?? null, error: null }
            : prev,
        );
        // Refresh silently: the persisted exchange (and the task id now on its row) replaces
        // the optimistic rows without flashing the skeleton.
        void load({ silent: true });
      } catch (e) {
        setConfirm((prev) =>
          prev && prev.token === token
            ? { ...prev, status: "failed", error: errorDisplay(e) }
            : prev,
        );
      }
    },
    [load],
  );

  return {
    items,
    total,
    /** How many rendered rows are still waiting for a history read to confirm them (F-38). */
    unconfirmed: items.filter((item) => item.local).length,
    status,
    error,
    sending,
    sendError,
    confirm,
    load,
    send,
    confirmAction,
  };
}
