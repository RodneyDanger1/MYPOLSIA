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
 *  - The confirm gate is separate state: it survives the silent history refresh that follows a
 *    successful confirm (the wire rows carry no token, so a reload cannot re-offer one).
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
 *  known branches (answer | create_task | refused); per-branch extras ride the same object. */
export type ChatReply = {
  reply: { text: string };
  intent: { kind: string; [key: string]: unknown };
  task_id: number | null;
  requires_confirm: boolean;
  confirm_token: string | null;
  refusal: ChatRefusal | null;
};

/** The 200 body of POST /api/v1/chat/confirm. */
export type ChatConfirmAck = { task_id: number };

/** A row as this rail holds it: the wire row plus the live-only extras the 201 body carries.
 *  A history read does NOT carry intent/refusal/token (frozen contract), so after a reload a
 *  reply renders as its persisted text only — the extras exist while the session's own send
 *  made them, which is exactly what the wire supports. */
export type ChatItem = ChatMessageRow & { refusal?: ChatRefusal | null };

/** The single confirm gate this rail can offer. Confirm tokens are single-use and history rows
 *  do not carry one, so at most the latest confirmable reply is actionable; an earlier unused
 *  token stays valid server-side until it is used or expires. */
export type ChatConfirmGate = {
  token: string;
  status: "pending" | "confirming" | "confirmed" | "failed";
  taskId: number | null;
  error: string | null;
};

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

  const load = useCallback(async ({ silent = false }: { silent?: boolean } = {}) => {
    const seq = ++readSeq.current;
    if (!silent) {
      setStatus("loading");
      setError(null);
    }
    try {
      const data = await api.get<{ items: ChatMessageRow[]; total: number }>(HISTORY_PATH);
      if (seq !== readSeq.current) return; // a newer read already landed — drop this one
      setItems(Array.isArray(data?.items) ? data.items : []);
      setTotal(typeof data?.total === "number" ? data.total : null);
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
      setItems((prev) => [
        ...prev,
        { role: "owner", content: text, created_at: now, task_id: null },
        {
          role: "polsia",
          content: reply.reply.text,
          created_at: now,
          task_id: reply.task_id ?? null,
          refusal: reply.refusal ?? null,
        },
      ]);
      setTotal((prev) => (prev === null ? null : prev + 2));
      if (reply.requires_confirm && reply.confirm_token) {
        setConfirm({
          token: reply.confirm_token,
          status: "pending",
          taskId: null,
          error: null,
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
            ? { token, status: "confirmed", taskId: ack?.task_id ?? null, error: null }
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

  return { items, total, status, error, sending, sendError, confirm, load, send, confirmAction };
}
