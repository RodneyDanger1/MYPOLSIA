/**
 * F-46 (wave 8) — WHICH stored "Confirm and I'll queue it" reply still has a live gate.
 *
 * THE DEFECT (visuals `VISUALS-FINDINGS-W7.md` §4, measured live on `dc78e80`): the transcript
 * renders every persisted reply verbatim, and 21 of them end with the instruction *"Confirm and
 * I'll queue it"* while the rail holds exactly ONE confirm gate at most. The server stopped minting
 * those old gates (wave 7: a new proposal marks every older pending proposal `superseded`; the
 * restore read only ever re-issues the NEWEST one, and only inside its original 15-minute window),
 * but the stored text was never rewritten — and it must not be: history is history. So the SCREEN
 * has to tell the truth about the instruction it is showing.
 *
 * WHAT THE WIRE ALLOWS (GET /chat, `ChatHistoryResponse`):
 *   • rows carry `{role, content, created_at, task_id}` — no id, no intent, no per-row state, so a
 *     row cannot be matched to `pending_confirm.chat_message_id` directly;
 *   • `pending_confirm` is `null` unless the NEWEST pending proposal's original window is still
 *     open, and then it is the ONE live gate.
 *
 * THE RULE THIS MODULE ENCODES (and why it is exact, not a guess):
 *   • every `create_task` reply is minted with the instruction (chat_service's reply template), and
 *     the history read returns the NEWEST `limit` rows in reading order — so when a gate is live,
 *     its reply is necessarily in the window and is the newest instruction-bearing row;
 *   • when the gate is null, NO instruction row can be live;
 *   • a CONFIRMED gate is spent: its card reports the task, and the row it belonged to is history
 *     too (so the "newest" exemption does not apply once `status === "confirmed"`).
 *
 * Everything is a narrowing of values the server actually sent: a junk/absent content is not an
 * instruction, and a junk gate is "no gate" — never an invented liveness claim.
 */

/** Just the fields this decision needs — `ChatItem` satisfies it structurally. */
export type ConfirmInstructionRow = {
  role: string;
  content: unknown;
};

/** The confirm gate's status field (`ChatConfirmGate.status`), narrowed to what liveness needs. */
export type ConfirmGateStatus = {
  status: string;
};

/** The EXACT instruction the backend's `create_task` reply carries (chat_service.py's template).
 *  Matched case-insensitively and with either apostrophe so a text-normalising pass cannot make a
 *  stored instruction invisible (the live rows use `'`; a copy-paste route may use `’`). */
const CONFIRM_INSTRUCTION = /confirm and i['’]ll queue it/i;

/** Does this row's text carry the confirm instruction? A non-string is NOT an instruction — the
 *  row is rendered as-is and gets no gate claim. */
export function carriesConfirmInstruction(content: unknown): boolean {
  return typeof content === "string" && CONFIRM_INSTRUCTION.test(content);
}

/** Indexes of the `polsia` rows that carry the instruction, in the order given (oldest → newest). */
export function confirmInstructionIndexes(items: ConfirmInstructionRow[]): number[] {
  const out: number[] = [];
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if (!item || item.role !== "polsia") continue;
    if (carriesConfirmInstruction(item.content)) out.push(index);
  }
  return out;
}

/** Whether the rail's current gate still OFFERS a CONFIRM control. `confirmed` is spent (the card
 *  below reports the task); `pending` / `confirming` / `failed` still offer it (`failed` keeps the
 *  token retryable until it expires). A junk status is treated as no gate. */
export function gateOffersConfirm(gate: ConfirmGateStatus | null | undefined): boolean {
  if (!gate || typeof gate.status !== "string") return false;
  return gate.status === "pending" || gate.status === "confirming" || gate.status === "failed";
}

/** The index (into `items`) of the row the live gate belongs to, or -1 when nothing is live.
 *
 *  Exactness: the live gate is the newest pending proposal's, whose reply is the newest
 *  instruction-bearing row in the served window (see the module docblock). A confirmed gate yields
 *  -1 — its instruction is history like every other. */
export function liveGateIndex(
  items: ConfirmInstructionRow[],
  gate: ConfirmGateStatus | null | undefined,
): number {
  if (!gateOffersConfirm(gate)) return -1;
  const indexes = confirmInstructionIndexes(items);
  return indexes.length > 0 ? indexes[indexes.length - 1] : -1;
}

/**
 * The ONE-live-gate sentence the rail shows next to the gate (F-46), or `null` when there is
 * neither an instruction on screen nor a gate to describe.
 *
 * The text is derived ONLY from what the wire proved: how many instruction replies are rendered,
 * whether a gate offers CONFIRM, and (after a confirmation) the task id the server returned. It
 * never says a specific instruction "expired at 06:25" — the history read does not carry that
 * instant for a superseded/consumed row, so the sentence names the possible endings instead of
 * inventing one.
 *
 * COPY RULE (wave 8): this sentence never repeats the stored instruction verbatim
 * ("Confirm and I'll queue it") and never repeats the wave-7 reply's advice ("send the request
 * again"). The rail already renders those strings from history; duplicating them in the chrome
 * made a reader (and a `getByText`) unable to tell the server's record from the app's commentary.
 */
export function gateStatement({
  instructionCount,
  liveIndex,
  confirmed,
  confirmedTaskId,
}: {
  instructionCount: number;
  liveIndex: number;
  confirmed: boolean;
  confirmedTaskId: number | null;
}): string | null {
  if (instructionCount === 0) {
    return liveIndex >= 0 ? "ONE live confirm gate: its card is below." : null;
  }
  if (liveIndex >= 0) {
    const older = instructionCount - 1;
    if (older === 0) {
      return "ONE live confirm gate: the newest proposal, whose card is below.";
    }
    const earlier =
      older === 1
        ? "The 1 earlier confirm reply in this transcript is history — its window closed or a " +
          "newer proposal superseded it — and it cannot queue anything."
        : `The ${older} earlier confirm replies in this transcript are history — their windows ` +
          "closed or a newer proposal superseded them — and none of them can queue anything.";
    return `ONE live confirm gate: the newest proposal (card below). ${earlier}`;
  }
  if (confirmed) {
    const replies = instructionCount === 1 ? "confirm reply is" : "confirm replies are";
    const task = confirmedTaskId !== null ? ` — task #${confirmedTaskId} created` : "";
    return (
      `No confirm gate is live: the newest proposal was just confirmed${task} (the card below ` +
      `says so). All ${instructionCount} ${replies} now history and none can be confirmed again.`
    );
  }
  if (instructionCount === 1) {
    return (
      "No confirm gate is live. The only confirm reply in this transcript is history — its " +
      "single-use window has closed, a newer proposal superseded it, or it was already confirmed. " +
      "No CONFIRM is offered and it cannot queue anything; ask again and the server replies with a " +
      "fresh card."
    );
  }
  return (
    `No confirm gate is live. All ${instructionCount} confirm replies in this transcript are ` +
    "history — their single-use windows have closed, a newer proposal superseded them, or they " +
    "were already confirmed. No CONFIRM is offered and none of them can queue anything; ask again " +
    "and the server replies with a fresh card."
  );
}
