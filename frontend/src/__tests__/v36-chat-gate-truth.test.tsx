/**
 * WAVE 8 / F-46 — a stored "Confirm and I'll queue it" reply must not point at a control that is
 * not there.
 *
 * THE DEFECT (visuals `VISUALS-FINDINGS-W7.md` §4; re-measured live on `dc78e80` 2026-10-29):
 * `GET /chat` returned 50 stored rows of which **21** carried the confirm instruction, while the
 * server held **no** live gate (`pending_confirm: null`) and the rail rendered 0 cards / 0 CONFIRM
 * buttons. The text is history and MUST NOT be rewritten — the screen has to state the truth about
 * it instead.
 *
 * THE RULE UNDER TEST (`components/chat/confirmGateTruth.ts`): at most ONE instruction is live —
 * the newest one, and only while the gate still offers CONFIRM. Every other instruction-bearing
 * row is marked, at the row, as history; the rail states "no gate is live" (with the count) when
 * none is; and a confirmed gate marks its own instruction as spent.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() } };
});

import { api } from "@/lib/api";
import { ChatRail } from "@/components/chat/ChatRail";
import {
  carriesConfirmInstruction,
  confirmInstructionIndexes,
  gateOffersConfirm,
  gateStatement,
  liveGateIndex,
} from "@/components/chat/confirmGateTruth";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const MINUTE_MS = 60 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
const EXPIRES_AT = new Date(Date.now() + 8 * MINUTE_MS).toISOString();

/** One stored `create_task` reply, exactly the shape the backend persists (the wave-7 copy says
 *  the window closes too — the OLD rows do not; both must be told apart from a live gate). */
function storedInstruction(tag: string, { wave7Copy = false } = {}): Record<string, unknown> {
  return {
    role: "polsia",
    content:
      `Ready to file this for the orchestrator agent: "${tag}"\n` +
      "Confirm and I'll queue it — nothing is created until you confirm (single-use token, " +
      "expires in 15 min)." +
      (wave7Copy
        ? " Reloading the page brings the card back while that token is still valid; once it " +
          "expires this stored message cannot queue anything — send the request again."
        : ""),
    created_at: ago(90 * MINUTE_MS),
    task_id: null,
  };
}

const OWNER_ROW = {
  role: "owner",
  content: "draft the launch checklist",
  created_at: ago(90 * MINUTE_MS),
  task_id: null,
};

const ANSWER_ROW = {
  role: "polsia",
  content: "Finance: MRR not measured; cash balance $0.00.",
  created_at: ago(80 * MINUTE_MS),
  task_id: null,
};

const RESTORED = {
  chat_message_id: 130,
  token: "130.1793000000.deadbeef",
  expires_at: EXPIRES_AT,
  intent: {
    kind: "create_task",
    agent_type: "business_planning",
    title: "draft the launch checklist",
    paid_run: true,
    run_cost_cap_usd: 2,
    day_cost_cap_usd: 25,
  },
};

const LIVE_REPLY = {
  reply: { text: 'Ready to file this.\nConfirm and I\'ll queue it — nothing is created until you confirm.' },
  intent: {
    kind: "create_task",
    agent_type: "business_planning",
    title: "draft the launch checklist",
    paid_run: true,
    run_cost_cap_usd: 2,
    day_cost_cap_usd: 25,
  },
  task_id: null,
  requires_confirm: true,
  confirm_token: "live-token-1",
  refusal: null,
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

describe("F-46 — the live rail: no gate exists, every stored instruction says so", () => {
  it("marks all three stored instructions and states that NO gate is live", async () => {
    mockGet.mockResolvedValue({
      items: [
        OWNER_ROW,
        storedInstruction("first"),
        storedInstruction("second"),
        ANSWER_ROW,
        storedInstruction("third", { wave7Copy: true }),
      ],
      total: 5,
      pending_confirm: null,
    });

    render(<ChatRail />);

    // The stored text is rendered VERBATIM (history is never rewritten)...
    const rail = await screen.findByTestId("chat-rail");
    const instructionRows = within(rail)
      .getAllByRole("listitem")
      .filter((row) => /Confirm and I'll queue it/.test(row.textContent ?? ""));
    expect(instructionRows).toHaveLength(3);

    // ...and every instruction-bearing row is marked as history.
    await waitFor(() => expect(screen.getAllByTestId("chat-confirm-inactive")).toHaveLength(3));
    // The plain answer row carries no gate claim.
    const answer = within(rail).getByText(/Finance: MRR not measured/).closest("li");
    expect(answer?.querySelector('[data-testid="chat-confirm-inactive"]')).toBeNull();

    // The ONE-live-gate fact, with the real count.
    const truth = screen.getByTestId("chat-gate-truth");
    expect(truth).toHaveTextContent(/No confirm gate is live/i);
    expect(truth).toHaveTextContent(/All 3 confirm replies/i);
    expect(truth).toHaveTextContent(/No CONFIRM is offered/i);
    expect(truth).toHaveTextContent(/ask again and the server replies with a fresh card/i);

    // No dead affordance: there is no CONFIRM to press.
    expect(screen.queryByRole("button", { name: "CONFIRM" })).toBeNull();
    expect(screen.queryByTestId("chat-confirm-card")).toBeNull();
  });

  it("uses the singular sentence when the transcript holds exactly one instruction", async () => {
    mockGet.mockResolvedValue({
      items: [OWNER_ROW, storedInstruction("only")],
      total: 2,
      pending_confirm: null,
    });
    render(<ChatRail />);

    const truth = await screen.findByTestId("chat-gate-truth");
    expect(truth).toHaveTextContent(/The only confirm reply/i);
    expect(screen.getAllByTestId("chat-confirm-inactive")).toHaveLength(1);
  });

  it("a malformed pending_confirm block is NO gate — never an invented liveness", async () => {
    for (const bad of [
      { ...RESTORED, token: "" },
      { ...RESTORED, intent: { kind: "answer" } },
      { token: "x" },
    ]) {
      mockGet.mockReset();
      mockGet.mockResolvedValue({
        items: [OWNER_ROW, storedInstruction("bad")],
        total: 2,
        pending_confirm: bad,
      });
      const { unmount } = render(<ChatRail />);
      await screen.findByTestId("chat-gate-truth");
      expect(screen.getAllByTestId("chat-confirm-inactive")).toHaveLength(1);
      expect(screen.getByTestId("chat-gate-truth")).toHaveTextContent(/No confirm gate is live/i);
      expect(screen.queryByTestId("chat-confirm-card")).toBeNull();
      unmount();
    }
  });
});

describe("F-46 — exactly one live gate", () => {
  it("exempts only the NEWEST instruction and marks the older one as history", async () => {
    mockGet.mockResolvedValue({
      items: [OWNER_ROW, storedInstruction("older"), OWNER_ROW, storedInstruction("newest")],
      total: 4,
      pending_confirm: RESTORED,
    });

    render(<ChatRail />);

    const card = await screen.findByTestId("chat-confirm-card");
    expect(within(card).getByRole("button", { name: "CONFIRM" })).toBeEnabled();
    // Exactly one row is marked — the older instruction; the newest owns the card.
    expect(screen.getAllByTestId("chat-confirm-inactive")).toHaveLength(1);
    const rows = screen.getAllByRole("listitem");
    expect(rows[1].querySelector('[data-testid="chat-confirm-inactive"]')).not.toBeNull();
    expect(rows[3].querySelector('[data-testid="chat-confirm-inactive"]')).toBeNull();

    const truth = screen.getByTestId("chat-gate-truth");
    expect(truth).toHaveTextContent(/ONE live confirm gate/i);
    expect(truth).toHaveTextContent(/The 1 earlier confirm reply in this transcript is history/i);
    expect(truth).toHaveTextContent(/its window closed or a newer proposal superseded it/i);
  });

  it("rendering the restored gate confirms nothing", async () => {
    mockGet.mockResolvedValue({
      items: [OWNER_ROW, storedInstruction("only")],
      total: 2,
      pending_confirm: RESTORED,
    });
    render(<ChatRail />);
    await screen.findByTestId("chat-confirm-card");
    expect(mockPost).not.toHaveBeenCalled();
    // The single instruction is the live one: no history marker.
    expect(screen.queryByTestId("chat-confirm-inactive")).toBeNull();
  });

  it("a CONFIRMED gate is spent: its own instruction is history and the task is named", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValueOnce({ items: [], total: 0 });
    mockGet.mockResolvedValue({ items: [], total: 0 });
    mockPost.mockResolvedValueOnce(LIVE_REPLY).mockResolvedValueOnce({ task_id: 77 });

    render(<ChatRail />);
    await screen.findByText(/No messages yet/);
    await user.type(screen.getByLabelText("Message"), "draft the launch checklist");
    await user.click(screen.getByRole("button", { name: "SEND" }));

    const card = await screen.findByTestId("chat-confirm-card");
    await user.click(within(card).getByRole("button", { name: "CONFIRM" }));
    expect(await screen.findByText("Task #77 created.")).toBeInTheDocument();

    // The instruction the confirm consumed is now history — marked, not silently re-offered.
    await waitFor(() => expect(screen.getByTestId("chat-confirm-inactive")).toBeInTheDocument());
    const truth = screen.getByTestId("chat-gate-truth");
    expect(truth).toHaveTextContent(/No confirm gate is live/i);
    expect(truth).toHaveTextContent(/just confirmed/i);
    expect(truth).toHaveTextContent(/task #77/i);
    expect(screen.queryByRole("button", { name: "CONFIRM" })).toBeNull();
  });
});

describe("F-46 — the narrowing rules (pure units)", () => {
  it("detects the instruction in either apostrophe and ignores anything else", () => {
    expect(carriesConfirmInstruction("Confirm and I'll queue it")).toBe(true);
    expect(carriesConfirmInstruction("confirm and I’ll queue it now")).toBe(true);
    expect(carriesConfirmInstruction("Confirm and I WILL queue it")).toBe(false);
    expect(carriesConfirmInstruction("I can't offer a confirmation card")).toBe(false);
    expect(carriesConfirmInstruction(null)).toBe(false);
    expect(carriesConfirmInstruction(42)).toBe(false);
    expect(carriesConfirmInstruction(undefined)).toBe(false);
  });

  it("indexes only polsia rows, in order", () => {
    const items = [
      { role: "owner", content: "Confirm and I'll queue it" },
      { role: "polsia", content: "Confirm and I'll queue it (a)" },
      { role: "polsia", content: "an answer" },
      { role: "polsia", content: "Confirm and I'll queue it (b)" },
    ];
    expect(confirmInstructionIndexes(items)).toEqual([1, 3]);
  });

  it("only a gate that still offers CONFIRM is live", () => {
    expect(gateOffersConfirm({ status: "pending" })).toBe(true);
    expect(gateOffersConfirm({ status: "confirming" })).toBe(true);
    expect(gateOffersConfirm({ status: "failed" })).toBe(true);
    expect(gateOffersConfirm({ status: "confirmed" })).toBe(false);
    expect(gateOffersConfirm(null)).toBe(false);
    expect(gateOffersConfirm({ status: "someday" })).toBe(false);
  });

  it("liveGateIndex is the NEWEST instruction, or -1", () => {
    const items = [
      { role: "polsia", content: "Confirm and I'll queue it (a)" },
      { role: "owner", content: "x" },
      { role: "polsia", content: "Confirm and I'll queue it (b)" },
    ];
    expect(liveGateIndex(items, { status: "pending" })).toBe(2);
    expect(liveGateIndex(items, null)).toBe(-1);
    expect(liveGateIndex(items, { status: "confirmed" })).toBe(-1);
    expect(liveGateIndex([], { status: "pending" })).toBe(-1);
  });

  it("gateStatement says exactly what the counts prove — and nothing when there is nothing", () => {
    expect(gateStatement({ instructionCount: 0, liveIndex: -1, confirmed: false, confirmedTaskId: null })).toBeNull();
    expect(gateStatement({ instructionCount: 0, liveIndex: 0, confirmed: false, confirmedTaskId: null })).toMatch(
      /ONE live confirm gate/,
    );
    expect(gateStatement({ instructionCount: 1, liveIndex: 0, confirmed: false, confirmedTaskId: null })).toMatch(
      /whose card is below/,
    );
    expect(gateStatement({ instructionCount: 2, liveIndex: 1, confirmed: false, confirmedTaskId: null })).toMatch(
      /The 1 earlier/,
    );
    expect(gateStatement({ instructionCount: 3, liveIndex: 2, confirmed: false, confirmedTaskId: null })).toMatch(
      /The 2 earlier confirm replies in this transcript are history/,
    );
    expect(gateStatement({ instructionCount: 3, liveIndex: -1, confirmed: true, confirmedTaskId: 77 })).toMatch(
      /task #77/,
    );
    // Confirmed takes precedence over the "none" copy, even for a single instruction.
    expect(gateStatement({ instructionCount: 1, liveIndex: -1, confirmed: true, confirmedTaskId: null })).toMatch(
      /just confirmed/,
    );
    expect(gateStatement({ instructionCount: 1, liveIndex: -1, confirmed: false, confirmedTaskId: null })).toMatch(
      /The only .* reply in this transcript is history/,
    );
    // No invented instant for a superseded/expired gate (the read carries no per-row state).
    const none = gateStatement({ instructionCount: 2, liveIndex: -1, confirmed: false, confirmedTaskId: null });
    expect(none).not.toMatch(/\d{1,2}:\d{2}/);
  });
});
