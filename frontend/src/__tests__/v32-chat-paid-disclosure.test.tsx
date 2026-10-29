/**
 * WAVE 4 / F-29 — the chat "FILE THIS TASK" confirm must disclose the PAID run BEFORE the click.
 *
 * THE DEFECT: `POST /chat/confirm` creates the task AND hands it to a worker
 * (`run_agent_task.delay`), so one click starts a model run that spends money. The card said only
 * "CONFIRM files it through the same path the agents page uses" — no mention of a paid run, a cap
 * or the absence of an estimate.
 *
 * THE FIX under test: the 201 reply carries `intent.paid_run` / `run_cost_cap_usd` /
 * `day_cost_cap_usd` (from the accounting policy the worker enforces); the rail captures them into
 * the confirm gate and the card states them between the sentence and the button. A server that
 * predates F-29 gets "did not declare the caps" — never an invented number. Rendering the card
 * still confirms nothing, and the confirm POST still carries only the token.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() } };
});

import { api } from "@/lib/api";
import { ChatRail } from "@/components/chat/ChatRail";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const CREATE_WITH_CAPS = {
  reply: { text: "Ready to file this for the business_planning agent." },
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
  confirm_token: "tok_f29",
  refusal: null,
};

const CREATE_WITHOUT_CAPS = {
  reply: { text: "Ready to file this." },
  intent: { kind: "create_task", agent_type: "business_planning", title: "File me" },
  task_id: null,
  requires_confirm: true,
  confirm_token: "tok_f29_nocaps",
  refusal: null,
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockGet.mockResolvedValue({ items: [], total: 0 });
});

async function openGate(reply: unknown): Promise<void> {
  const user = userEvent.setup();
  mockPost.mockResolvedValueOnce(reply).mockResolvedValueOnce({ task_id: 42 });
  render(<ChatRail />);
  await screen.findByText(/No messages yet/);
  await user.type(screen.getByLabelText("Message"), "draft the launch checklist");
  await user.click(screen.getByRole("button", { name: "SEND" }));
  await screen.findByTestId("chat-confirm-card");
}

it("states the paid run and the server's caps before the click", async () => {
  await openGate(CREATE_WITH_CAPS);

  const card = await screen.findByTestId("chat-confirm-card");
  const disclosure = within(card).getByTestId("chat-paid-run-disclosure");
  expect(disclosure).toHaveTextContent(/PAID agent run/);
  expect(within(disclosure).getByTestId("chat-run-cost-cap")).toHaveTextContent("$2.00 per run");
  expect(within(disclosure).getByTestId("chat-day-cost-cap")).toHaveTextContent("$25.00 per day");
  expect(disclosure).toHaveTextContent(/measured after the run/i);
  expect(disclosure).toHaveTextContent(/no estimate/i);

  // The disclosure is ABOVE the button in reading order.
  const button = within(card).getByRole("button", { name: "CONFIRM" });
  expect(
    disclosure.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();

  // Nothing has been confirmed by rendering it.
  expect(mockPost).toHaveBeenCalledTimes(1);
});

it("still discloses the paid run when the server declared no caps — never invents numbers", async () => {
  await openGate(CREATE_WITHOUT_CAPS);

  const card = await screen.findByTestId("chat-confirm-card");
  const disclosure = within(card).getByTestId("chat-paid-run-disclosure");
  expect(disclosure).toHaveTextContent(/real agent run/);
  expect(disclosure).toHaveTextContent(/spends money/i);
  expect(within(disclosure).getByTestId("chat-cost-cap-undeclared")).toHaveTextContent(
    /did not declare the run.s cost caps/i,
  );
  expect(within(disclosure).queryByTestId("chat-run-cost-cap")).toBeNull();
  expect(within(card).queryByText(/\$\d/)).toBeNull();
});

it("CONFIRM sends only the token — the disclosure changed no wire payload", async () => {
  const user = userEvent.setup();
  mockPost.mockResolvedValueOnce(CREATE_WITH_CAPS).mockResolvedValueOnce({ task_id: 42 });

  render(<ChatRail />);
  await screen.findByText(/No messages yet/);
  await user.type(screen.getByLabelText("Message"), "draft the launch checklist");
  await user.click(screen.getByRole("button", { name: "SEND" }));

  const card = await screen.findByTestId("chat-confirm-card");
  await user.click(within(card).getByRole("button", { name: "CONFIRM" }));

  expect(mockPost).toHaveBeenNthCalledWith(2, "/chat/confirm", { confirm_token: "tok_f29" });
  expect(await screen.findByText("Task #42 created.")).toBeInTheDocument();
  await waitFor(() =>
    expect(mockGet.mock.calls.filter(([path]) => String(path).startsWith("/chat")).length).toBe(2),
  );
});
