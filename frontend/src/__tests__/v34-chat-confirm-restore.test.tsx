/**
 * WAVE 6 / F-40 — the confirm gate is restored from the STORED proposal after a reload.
 *
 * THE DEFECT (visuals `VISUALS-FINDINGS-W5.md` §4, live on `7b81f56`): a chat proposal rendered the
 * reply "Confirm and I'll queue it — nothing is created until you confirm (single-use token,
 * expires in 15 min)" plus a confirm card. After ANY reload the card and its CONFIRM button were
 * gone (`chat-confirm-card` nodes = 0) while the message still gave that instruction — the owner
 * lost the request and the guidance pointed at a control that did not exist.
 *
 * THE FIX under test: `GET /api/v1/chat` carries the ADDITIVE `pending_confirm` block — the still
 * open single-use token for the newest stored proposal, with the same `create_task` facts the 201
 * served — and the rail rebuilds the card from it. A read may only FILL an empty gate, never
 * overwrite one this session holds (a live send, a confirmation, or a failed attempt).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() } };
});

import { api } from "@/lib/api";
import { ChatRail } from "@/components/chat/ChatRail";
import { absoluteStamp } from "@/components/dashboard/format";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const MINUTE_MS = 60 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
const EXPIRES_AT = new Date(Date.now() + 8 * MINUTE_MS).toISOString();

const PROPOSAL_TEXT =
  "Ready to file this for the business_planning agent:\n" +
  '"Draft the launch checklist"\n' +
  "Confirm and I'll queue it — nothing is created until you confirm " +
  "(single-use token, expires in 15 min). Reloading the page brings the card back while that " +
  "token is still valid; once it expires this stored message cannot queue anything — send the " +
  "request again.";

/** The persisted exchange, exactly as the frozen history read returns it (plus F-40's block). */
const STORED_ROWS = {
  items: [
    {
      role: "owner",
      content: "draft the launch checklist",
      created_at: ago(5 * MINUTE_MS),
      task_id: null,
    },
    {
      role: "polsia",
      content: PROPOSAL_TEXT,
      created_at: ago(5 * MINUTE_MS),
      task_id: null,
    },
  ],
  total: 2,
};

const RESTORED = {
  chat_message_id: 41,
  token: "41.1793000000.deadbeef",
  expires_at: EXPIRES_AT,
  intent: {
    kind: "create_task",
    agent_type: "business_planning",
    title: "Draft the launch checklist",
    paid_run: true,
    run_cost_cap_usd: 2,
    day_cost_cap_usd: 25,
  },
};

const LIVE_REPLY = {
  reply: { text: 'Ready to file this.\nConfirm and I\'ll queue it.' },
  intent: {
    kind: "create_task",
    agent_type: "business_planning",
    title: "Draft the launch checklist",
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

it("restores the card, the restored line and the server's caps from the stored proposal", async () => {
  mockGet.mockResolvedValue({ ...STORED_ROWS, pending_confirm: RESTORED });

  render(<ChatRail />);

  // The stored message is on screen (that is what the owner sees after a reload)...
  expect(await screen.findByText(/Confirm and I'll queue it/)).toBeInTheDocument();

  // ...and the affordance it instructs the owner to use is back.
  const card = await screen.findByTestId("chat-confirm-card");
  const button = within(card).getByRole("button", { name: "CONFIRM" });
  expect(button).toBeEnabled();

  // The card says where it came from and when the token dies — no pretending it is a fresh gate.
  const restored = within(card).getByTestId("chat-confirm-restored");
  expect(restored).toHaveTextContent(/Restored from the stored proposal/i);
  expect(restored).toHaveTextContent(new RegExp(absoluteStamp(EXPIRES_AT).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  expect(restored).toHaveTextContent(/same pending request/i);

  // The paid-run disclosure travels on the restored block, not a client-side default.
  const disclosure = within(card).getByTestId("chat-paid-run-disclosure");
  expect(within(disclosure).getByTestId("chat-run-cost-cap")).toHaveTextContent("$2.00 per run");
  expect(within(disclosure).getByTestId("chat-day-cost-cap")).toHaveTextContent("$25.00 per day");

  // Rendering the restored card confirms nothing.
  expect(mockPost).not.toHaveBeenCalled();
});

it("CONFIRM on the restored card sends the stored token and files the task", async () => {
  const user = userEvent.setup();
  mockGet.mockResolvedValue({ ...STORED_ROWS, pending_confirm: RESTORED });
  mockPost.mockResolvedValueOnce({ task_id: 77 });

  render(<ChatRail />);
  const card = await screen.findByTestId("chat-confirm-card");
  await user.click(within(card).getByRole("button", { name: "CONFIRM" }));

  expect(mockPost).toHaveBeenCalledTimes(1);
  expect(mockPost).toHaveBeenCalledWith("/chat/confirm", { confirm_token: RESTORED.token });
  expect(await screen.findByText("Task #77 created.")).toBeInTheDocument();
});

it("a stored proposal with no open gate renders the text and NO card", async () => {
  // The token window closed (or it was consumed): the server sends `pending_confirm: null`.
  mockGet.mockResolvedValue({ ...STORED_ROWS, pending_confirm: null });

  render(<ChatRail />);

  expect(await screen.findByText(/send the request again/i)).toBeInTheDocument();
  expect(screen.queryByTestId("chat-confirm-card")).toBeNull();
  expect(screen.queryByRole("button", { name: "CONFIRM" })).toBeNull();
});

it("a malformed pending_confirm block never becomes a card", async () => {
  for (const bad of [
    { ...RESTORED, token: "" },
    { ...RESTORED, token: 42 },
    { ...RESTORED, intent: { kind: "answer" } },
    { ...RESTORED, intent: null },
    { token: "x", expires_at: EXPIRES_AT },
  ]) {
    mockGet.mockReset();
    mockGet.mockResolvedValue({ ...STORED_ROWS, pending_confirm: bad });
    const { unmount } = render(<ChatRail />);
    await screen.findByText(/Confirm and I'll queue it/);
    await waitFor(() => expect(screen.queryByTestId("chat-confirm-card")).toBeNull());
    unmount();
  }
});

it("a history read never overwrites a gate this session already confirmed", async () => {
  const user = userEvent.setup();
  // Mount: nothing stored yet. The session sends and confirms LIVE...
  mockGet.mockResolvedValueOnce({ items: [], total: 0 });
  // ...then the silent post-confirm read returns a (stale) restore block. The confirmed gate wins.
  mockGet.mockResolvedValue({ ...STORED_ROWS, pending_confirm: RESTORED });
  mockPost.mockResolvedValueOnce(LIVE_REPLY).mockResolvedValueOnce({ task_id: 42 });

  render(<ChatRail />);
  await screen.findByText(/No messages yet/);
  await user.type(screen.getByLabelText("Message"), "draft the launch checklist");
  await user.click(screen.getByRole("button", { name: "SEND" }));

  const card = await screen.findByTestId("chat-confirm-card");
  await user.click(within(card).getByRole("button", { name: "CONFIRM" }));

  expect(await screen.findByText("Task #42 created.")).toBeInTheDocument();
  // The card stays the confirmed one — no CONFIRM button is offered again, no restored line.
  await waitFor(() =>
    expect(mockGet.mock.calls.filter(([path]) => String(path).startsWith("/chat")).length).toBe(2),
  );
  expect(screen.queryByRole("button", { name: "CONFIRM" })).toBeNull();
  expect(screen.queryByTestId("chat-confirm-restored")).toBeNull();
});
