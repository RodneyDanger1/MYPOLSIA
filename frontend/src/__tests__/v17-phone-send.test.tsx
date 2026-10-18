/**
 * WAVE V17 / lane L2 — the phone SEND composer (frozen `POST /api/v1/phone/send`).
 *
 * Written BEFORE the composer exists (RED leg raw in wave-v17/l2-phone-send/raw/). The pins below
 * are the honesty law for a surface that can text a real person:
 *
 *   - a refusal renders BY NAME with the server's own message and fix hint — never a generic
 *     "something went wrong", never a silent no-op;
 *   - a sandbox / unpaired / unavailable refusal DISABLES the composer and states the named reason
 *     (the attempt is what teaches the UI the state — there is no new GET route);
 *   - a validation refusal leaves the form usable, because fixing the text is the fix;
 *   - success shows the server's `sent_at` clock and the MASKED recipient — the body is never
 *     echoed back and no "Sent!" appears before the server answered;
 *   - while the request is in flight the button says "Sending…" and nothing claims a send.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn() }, API_KEY: "test-key" };
});

const mockSearch = { thread: null as string | null };

jest.mock("next/navigation", () => ({
  useSearchParams: () => {
    const params = new URLSearchParams();
    if (mockSearch.thread !== null) params.set("thread", mockSearch.thread);
    return params;
  },
}));

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink(props: { href: string; children: React.ReactNode } & Record<string, unknown>) {
    const { href, children, ...rest } = props;
    return (
      <a href={href} {...(rest as Record<string, string>)}>
        {children}
      </a>
    );
  },
}));

import { ApiError, api } from "@/lib/api";
import { PhoneConsole } from "@/components/phone/PhoneConsole";

const mockApiGet = api.get as jest.Mock;
const mockApiPost = api.post as jest.Mock;

const ok = (data: unknown) => ({ ok: true, data });

const SUMMARY = {
  store_status: "ok",
  unread_total: 1,
  threads: [
    { thread_id: 11, with: "Dana Whitfield", unread: 1, last_when: "Today 14:02", last_text: "See you at 6" },
    { thread_id: 42, with: "Mitch Alvarez", unread: 0, last_when: null, last_text: "thanks" },
  ],
  missed_calls_today: [],
};

function answerReads() {
  mockApiGet.mockImplementation((path: string) => {
    if (path.startsWith("/phone/summary")) return Promise.resolve(ok(SUMMARY));
    if (path.startsWith("/phone/messages")) return Promise.resolve(ok({ messages: [] }));
    if (path.startsWith("/phone/calls")) return Promise.resolve(ok({ calls: [] }));
    return Promise.reject(new Error(`unexpected read: ${path}`));
  });
}

async function fillAndSend(to = "11", body = "Running late") {
  fireEvent.change(await screen.findByTestId("phone-send-to"), { target: { value: to } });
  fireEvent.change(screen.getByTestId("phone-send-body"), { target: { value: body } });
  fireEvent.click(screen.getByTestId("phone-send-submit"));
}

beforeEach(() => {
  mockApiGet.mockReset();
  mockApiPost.mockReset();
  mockSearch.thread = null;
  answerReads();
});

describe("phone send composer — honest refusals", () => {
  it("E_SANDBOX_BLOCKED renders by name and DISABLES the composer with the named reason", async () => {
    mockApiPost.mockRejectedValue(
      new ApiError(
        403,
        "phone send refused: SANDBOX_MODE is on, so no real SMS is sent from this deployment",
        "E_SANDBOX_BLOCKED",
        "set SANDBOX_MODE=false once the phone pairing is configured",
      ),
    );
    render(<PhoneConsole />);
    await fillAndSend();

    const error = await screen.findByTestId("phone-send-error");
    expect(error).toHaveTextContent("E_SANDBOX_BLOCKED");
    expect(error).toHaveTextContent("SANDBOX_MODE");
    expect(screen.getByTestId("phone-send-submit")).toBeDisabled();
    expect(screen.getByTestId("phone-send-blocked")).toHaveTextContent("E_SANDBOX_BLOCKED");
    expect(screen.queryByTestId("phone-send-result")).toBeNull();
    expect(screen.queryByText(/sent at/i)).toBeNull();
  });

  it("E_PHONE_SEND_UNPAIRED names the pairing gap and disables the composer", async () => {
    mockApiPost.mockRejectedValue(
      new ApiError(
        409,
        "no messages.google.com/web tab is open in the automation Chrome",
        "E_PHONE_SEND_UNPAIRED",
        "pair the phone: open Google Messages web in the automation Chrome and scan the QR code once",
      ),
    );
    render(<PhoneConsole />);
    await fillAndSend("+15550104477", "hi");

    const error = await screen.findByTestId("phone-send-error");
    expect(error).toHaveTextContent("E_PHONE_SEND_UNPAIRED");
    expect(error).toHaveTextContent("no messages.google.com/web tab");
    expect(screen.getByTestId("phone-send-submit")).toBeDisabled();
    expect(screen.getByTestId("phone-send-blocked")).toHaveTextContent("pair");
    expect(screen.queryByTestId("phone-send-result")).toBeNull();
  });

  it("E_PHONE_UNAVAILABLE disables the composer and says the driver is unreachable", async () => {
    mockApiPost.mockRejectedValue(
      new ApiError(503, "CDP is not reachable on 127.0.0.1:9223", "E_PHONE_UNAVAILABLE", ""),
    );
    render(<PhoneConsole />);
    await fillAndSend();

    const error = await screen.findByTestId("phone-send-error");
    expect(error).toHaveTextContent("E_PHONE_UNAVAILABLE");
    expect(error).toHaveTextContent("9223");
    expect(screen.getByTestId("phone-send-submit")).toBeDisabled();
    expect(screen.getByTestId("phone-send-blocked")).toHaveTextContent("E_PHONE_UNAVAILABLE");
  });

  it("E_VALIDATION names the field and leaves the form usable (fixing the text IS the fix)", async () => {
    mockApiPost.mockRejectedValue(
      new ApiError(422, "`body` is 700 characters; this build sends at most 640", "E_VALIDATION", "shorten the message"),
    );
    render(<PhoneConsole />);
    await fillAndSend("11", "a".repeat(640));

    const error = await screen.findByTestId("phone-send-error");
    expect(error).toHaveTextContent("E_VALIDATION");
    expect(error).toHaveTextContent("640");
    expect(screen.getByTestId("phone-send-submit")).not.toBeDisabled();
    expect(screen.queryByTestId("phone-send-blocked")).toBeNull();
    expect(screen.queryByTestId("phone-send-result")).toBeNull();
  });
});

describe("phone send composer — success claims only what the server observed", () => {
  it("renders `sent at HH:MM` with the masked recipient, and posts exactly {to, body}", async () => {
    mockApiPost.mockResolvedValue(
      ok({ sent_at: "2026-10-18T12:34:56-05:00", to_masked: "•••• 4477", driver: "cdp-google-messages" }),
    );
    render(<PhoneConsole />);
    await fillAndSend("+15550104477", "Running late, sorry!");

    const result = await screen.findByTestId("phone-send-result");
    expect(result).toHaveTextContent("Sent at 12:34");
    expect(result).toHaveTextContent("•••• 4477");
    expect(result).not.toHaveTextContent("5550104477");
    expect(mockApiPost).toHaveBeenCalledTimes(1);
    expect(mockApiPost).toHaveBeenCalledWith("/phone/send", {
      to: "+15550104477",
      body: "Running late, sorry!",
    });
    // A completed attempt clears the composer's text so a second tap cannot re-send it.
    expect(screen.getByTestId("phone-send-body")).toHaveValue("");
    expect(screen.queryByTestId("phone-send-error")).toBeNull();
  });

  it("never reports a send before the server answers", async () => {
    let resolvePost: (value: unknown) => void = () => undefined;
    mockApiPost.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePost = resolve;
        }),
    );
    render(<PhoneConsole />);
    await fillAndSend();

    const button = screen.getByTestId("phone-send-submit");
    await waitFor(() => expect(button).toHaveTextContent("Sending…"));
    expect(screen.queryByTestId("phone-send-result")).toBeNull();
    expect(screen.queryByText(/sent at/i)).toBeNull();
    expect(button).toBeDisabled();

    resolvePost(ok({ sent_at: "2026-10-18T09:05:00-05:00", to_masked: "•••• 9931", driver: "cdp-google-messages" }));
    expect(await screen.findByTestId("phone-send-result")).toHaveTextContent("Sent at 09:05");
  });

  it("a result naming an unknown driver is refused, not displayed as a send", async () => {
    mockApiPost.mockResolvedValue(
      ok({ sent_at: "2026-10-18T12:34:56-05:00", to_masked: "•••• 4477", driver: "someone-else" }),
    );
    render(<PhoneConsole />);
    await fillAndSend();

    const error = await screen.findByTestId("phone-send-error");
    expect(error).toHaveTextContent("cdp-google-messages");
    expect(screen.queryByTestId("phone-send-result")).toBeNull();
  });
});

describe("phone send composer — form state", () => {
  it("keeps Send disabled until both a target and a message exist", async () => {
    render(<PhoneConsole />);
    const button = await screen.findByTestId("phone-send-submit");
    expect(button).toBeDisabled();

    fireEvent.change(screen.getByTestId("phone-send-to"), { target: { value: "11" } });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByTestId("phone-send-body"), { target: { value: "hi" } });
    expect(button).not.toBeDisabled();
    expect(mockApiPost).not.toHaveBeenCalled();
  });

  it("prefills the target from ?thread=N (the page's deep link)", async () => {
    mockSearch.thread = "42";
    render(<PhoneConsole />);
    expect(await screen.findByTestId("phone-send-to")).toHaveValue("42");
  });

  it("caps the message at the contract's 640 characters in the textarea itself", async () => {
    render(<PhoneConsole />);
    expect(await screen.findByTestId("phone-send-body")).toHaveAttribute("maxLength", "640");
  });

  it("does not render the composer when the store is unavailable", async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path.startsWith("/phone/summary")) {
        return Promise.reject(new ApiError(503, "store not mounted", "E_PHONE_UNAVAILABLE", "mount it"));
      }
      return Promise.resolve(ok({ messages: [] }));
    });
    render(<PhoneConsole />);

    expect(await screen.findByTestId("phone-unavailable")).toHaveTextContent("PHONE LINK UNAVAILABLE");
    expect(screen.queryByTestId("phone-send-submit")).toBeNull();
    expect(screen.queryByTestId("phone-composer")).toBeNull();
    const block = screen.getByTestId("phone-unavailable");
    expect(within(block).queryByRole("textbox")).toBeNull();
  });
});
