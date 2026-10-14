/**
 * Wave V8 / seat c3-chat-ui — the chat rail (ChatRail + Composer + ConfirmCard + useChat).
 *
 * Consumes the FROZEN chat contract (plan §7 C1; wave plan "Frozen interface — CHAT"):
 *   POST /api/v1/chat         {message} → 201 {reply:{text}, intent:{kind, extras}, task_id,
 *                             requires_confirm, confirm_token, refusal:{code,message,how_to_fix}|null}
 *   POST /api/v1/chat/confirm {confirm_token} → 200 {task_id} | 409 named refusal
 *   GET  /api/v1/chat?limit=50 → {items:[{role,content,created_at,task_id}], total}
 *
 * The API module is mocked the way the sibling suites do it (u2/u3): the rail is pinned on its
 * behaviour — what it sends, what it renders, which controls do real work — never on a live
 * backend. Fixtures carry the wire's own nulls (created_at nullable, task_id nullable) so the
 * honest placeholders are exercised, not invented.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import { ChatRail } from "@/components/chat/ChatRail";
import { absoluteStamp } from "@/components/dashboard/format";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const HOUR_MS = 60 * 60 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

/** The frozen GET history shape, in wire order (oldest → newest): both roles, real timestamps. */
const HISTORY = {
  items: [
    {
      role: "owner",
      content: "How many tasks ran today?",
      created_at: ago(2 * HOUR_MS + 30_000),
      task_id: null,
    },
    {
      role: "polsia",
      content: "Three tasks completed today; one is still waiting on your approval.",
      created_at: ago(HOUR_MS + 30_000),
      task_id: null,
    },
  ],
  total: 2,
};

const ANSWER_REPLY = {
  reply: { text: "Three tasks completed today." },
  intent: { kind: "answer" },
  task_id: null,
  requires_confirm: false,
  confirm_token: null,
  refusal: null,
};

const CREATE_TASK_REPLY = {
  reply: { text: "I can file that as a task: Draft the launch checklist." },
  intent: { kind: "create_task", title: "Draft the launch checklist" },
  task_id: null,
  requires_confirm: true,
  confirm_token: "tok_v8_confirm_1",
  refusal: null,
};

const REFUSED_REPLY = {
  reply: { text: "I can't move money — that stays a gated action." },
  intent: { kind: "refused" },
  task_id: null,
  requires_confirm: false,
  confirm_token: null,
  refusal: {
    code: "E_POLICY_DENIED",
    message: "Money and external sends are owner-gated.",
    how_to_fix: "Request an approval token on the Approvals path, then run the action yourself.",
  },
};

const historyGets = () =>
  mockGet.mock.calls.filter(([path]) => String(path).startsWith("/chat")).length;

afterEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

describe("chat rail (wave V8 / C3)", () => {
  it("renders the GET history — both roles, relative age and the absolute stamp in the title (T1)", async () => {
    mockGet.mockResolvedValue(HISTORY);

    render(<ChatRail />);

    // Owner right / polsia left is carried on data-role (the classes are presentation).
    const owner = await screen.findByText("How many tasks ran today?");
    const polsia = screen.getByText(
      "Three tasks completed today; one is still waiting on your approval.",
    );
    expect(owner.closest("li")).toHaveAttribute("data-role", "owner");
    expect(polsia.closest("li")).toHaveAttribute("data-role", "polsia");

    // Timestamps: relativeTime visible, absoluteStamp in the title, machine time in dateTime.
    const asked = within(owner.closest("li") as HTMLElement).getByText("2h ago");
    expect(asked).toHaveAttribute("title", absoluteStamp(HISTORY.items[0].created_at));
    expect(asked).toHaveAttribute("datetime", HISTORY.items[0].created_at);
    const answered = within(polsia.closest("li") as HTMLElement).getByText("1h ago");
    expect(answered).toHaveAttribute("title", absoluteStamp(HISTORY.items[1].created_at));

    // The read is the frozen path.
    expect(mockGet).toHaveBeenCalledWith("/chat?limit=50");
  });

  it("sending POSTs /api/v1/chat {message} and appends the reply (T2)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue({ items: [], total: 0 });
    mockPost.mockResolvedValue(ANSWER_REPLY);

    render(<ChatRail />);
    await screen.findByText(/No messages yet/);

    const box = screen.getByLabelText("Message");
    await user.type(box, "How many tasks ran today?");
    await user.click(screen.getByRole("button", { name: "SEND" }));

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith("/chat", { message: "How many tasks ran today?" });

    // The reply lands in the transcript, next to the message that earned it.
    expect(await screen.findByText("Three tasks completed today.")).toBeInTheDocument();
    expect(screen.getByText("How many tasks ran today?")).toBeInTheDocument();
    // The composer gives the box back for the next message only after success.
    await waitFor(() => expect(box).toHaveValue(""));
  });

  it("a create_task reply renders the ConfirmCard; CONFIRM POSTs /chat/confirm with the token (T3)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue({ items: [], total: 0 });
    mockPost.mockResolvedValueOnce(CREATE_TASK_REPLY).mockResolvedValueOnce({ task_id: 42 });

    render(<ChatRail />);
    await screen.findByText(/No messages yet/);
    await user.type(screen.getByLabelText("Message"), "Draft the launch checklist as a task");
    await user.click(screen.getByRole("button", { name: "SEND" }));
    expect(await screen.findByText(/I can file that as a task/)).toBeInTheDocument();

    // The gate: rendering the card confirms nothing.
    const card = await screen.findByTestId("chat-confirm-card");
    expect(mockPost).toHaveBeenCalledTimes(1); // only the /chat POST so far
    const confirmButton = within(card).getByRole("button", { name: "CONFIRM" });
    expect(confirmButton).toBeEnabled();

    await user.click(confirmButton);

    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(mockPost).toHaveBeenNthCalledWith(2, "/chat/confirm", {
      confirm_token: "tok_v8_confirm_1",
    });

    // Success shows the NEW task id and re-reads the history.
    expect(await screen.findByText("Task #42 created.")).toBeInTheDocument();
    await waitFor(() => expect(historyGets()).toBe(2));
  });

  it("a refused reply renders the code and the how-to-fix hint (T4)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue({ items: [], total: 0 });
    mockPost.mockResolvedValue(REFUSED_REPLY);

    render(<ChatRail />);
    await screen.findByText(/No messages yet/);
    await user.type(screen.getByLabelText("Message"), "Pay the contractor $900");
    await user.click(screen.getByRole("button", { name: "SEND" }));

    expect(await screen.findByText(/E_POLICY_DENIED/)).toBeInTheDocument();
    expect(screen.getByText(/Request an approval token on the Approvals path/)).toBeInTheDocument();
    // The reply text itself is in the transcript too — nothing was swallowed.
    expect(screen.getByText("I can't move money — that stays a gated action.")).toBeInTheDocument();
  });

  it("SEND is disabled while the input is empty or sending; Enter sends, Shift+Enter is a newline (T5)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue({ items: [], total: 0 });
    let resolvePost: ((value: unknown) => void) | null = null;
    mockPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePost = resolve;
        }),
    );

    render(<ChatRail />);
    await screen.findByText(/No messages yet/);

    const box = screen.getByLabelText("Message");
    expect(screen.getByRole("button", { name: "SEND" })).toBeDisabled();

    // Whitespace is not a message.
    await user.type(box, "   ");
    expect(screen.getByRole("button", { name: "SEND" })).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(mockPost).not.toHaveBeenCalled();

    await user.clear(box);
    await user.type(box, "status?");
    expect(screen.getByRole("button", { name: "SEND" })).toBeEnabled();

    // Shift+Enter never sends (it stays a line break)...
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(mockPost).not.toHaveBeenCalled();

    // ...plain Enter does, and the button disables + names the flight.
    await user.keyboard("{Enter}");
    expect(mockPost).toHaveBeenCalledTimes(1);
    const pending = screen.getByRole("button", { name: /sending/i });
    expect(pending).toBeDisabled();
    await user.click(pending); // a click during the flight must not double-send
    expect(mockPost).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolvePost?.(ANSWER_REPLY);
    });
    expect(await screen.findByText("Three tasks completed today.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "SEND" })).toBeDisabled());
  });

  it("has no dead buttons: SEND posts, CONFIRM confirms, RETRY refetches (T6)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue({ items: [], total: 0 });
    mockPost.mockResolvedValueOnce(CREATE_TASK_REPLY).mockResolvedValueOnce({ task_id: 7 });

    const first = render(<ChatRail />);
    await screen.findByText(/No messages yet/);

    // Every control that renders has a name...
    const names = screen.getAllByRole("button").map((button) => (button.textContent ?? "").trim());
    expect(names).toContain("SEND");
    for (const name of names) expect(name).not.toBe("");

    // ...and does real work: SEND fires the chat POST...
    await user.type(screen.getByLabelText("Message"), "file that task");
    await user.click(screen.getByRole("button", { name: "SEND" }));
    expect(mockPost).toHaveBeenNthCalledWith(1, "/chat", { message: "file that task" });

    // ...CONFIRM fires the confirm POST...
    await user.click(await screen.findByRole("button", { name: "CONFIRM" }));
    expect(mockPost).toHaveBeenNthCalledWith(2, "/chat/confirm", {
      confirm_token: "tok_v8_confirm_1",
    });
    first.unmount();

    // ...and the failure state's RETRY really re-reads (never a button that does nothing).
    mockGet.mockReset();
    let gets = 0;
    mockGet.mockImplementation(() => {
      gets += 1;
      return Promise.reject(
        new ApiError(503, "the chat store is unreachable", "E_DB", "check the database service"),
      );
    });
    render(<ChatRail />);
    const retry = await screen.findByRole("button", { name: "RETRY" });
    expect(gets).toBe(1);
    await user.click(retry);
    await waitFor(() => expect(gets).toBe(2));
  });

  it("renders skeleton → named failure (with RETRY) → the honest empty state, never a fake transcript", async () => {
    // Not read yet: skeleton, no empty claim, no alert.
    mockGet.mockImplementation(() => new Promise(() => {}));
    const { container, unmount } = render(<ChatRail />);
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(screen.queryByText(/No messages yet/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    unmount();

    // Failed read: named, with the retry affordance — and still no empty claim.
    mockGet.mockReset();
    mockGet.mockRejectedValue(
      new ApiError(503, "the chat store is unreachable", "E_DB", "check the database service"),
    );
    const failed = render(<ChatRail />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load chat history: E_DB");
    expect(alert).toHaveTextContent("check the database service");
    expect(screen.queryByText(/No messages yet/)).toBeNull();
    failed.unmount();

    // A successful empty read states the emptiness — the only time that sentence may render.
    mockGet.mockReset();
    mockGet.mockResolvedValue({ items: [], total: 0 });
    render(<ChatRail />);
    expect(await screen.findByText(/No messages yet/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
