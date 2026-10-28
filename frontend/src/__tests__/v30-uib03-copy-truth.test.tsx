/**
 * UIB-03 — the dashboard onboarding banner must not claim a runtime state it never read.
 *
 * The banner is driven by ONE read: `GET /config/deepseek-key`, which answers whether a model key
 * exists.  Whether dispatch is on is a DIFFERENT read the banner does not make (auto-mode off, the
 * beat stopped, `dispatch_paused: true` are all possible while the key is configured — measured on
 * the live box during this round: `/config/deepseek-key` `configured: true`, `/auto-mode`
 * `active: false`).  The old heading — "The agents are switched on" — was therefore a claim the
 * source could not support, and it is exactly the lying copy this test kills.
 *
 * Pinned:
 *   1. `configured: true` → the banner states the key fact + the planner step, and NEVER the
 *      unsupported "agents are switched on" claim;
 *   2. a failed/unknown status read → nothing is rendered (no claim in either direction).
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { render, screen, waitFor } from "@testing-library/react";

import { DeepSeekKeyOnboarding } from "@/components/dashboard/DeepSeekKeyOnboarding";
import { api } from "@/lib/api";

const mockGet = api.get as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
  try {
    window.localStorage.clear();
  } catch {
    // storage unavailable in this environment: the banner still has to be testable
  }
});

describe("UIB-03 — the onboarding copy is derived from the key status read", () => {
  it("says the key is configured and the planner is ready — never that the agents are switched on", async () => {
    mockGet.mockResolvedValue({ configured: true, source: "stored", masked: "sk-…1234" });

    render(<DeepSeekKeyOnboarding />);

    const banner = await screen.findByTestId("planner-onboarding");
    expect(banner).toHaveTextContent(/model key is configured/i);
    expect(banner).toHaveTextContent(/planner is ready/i);
    // The unsupported runtime claim is GONE: presence of a key does not prove dispatch is on.
    expect(banner).not.toHaveTextContent(/agents are switched on/i);
  });

  it("renders nothing when the key status read fails — no claim in either direction", async () => {
    mockGet.mockRejectedValue(new Error("E_CONFIG: the key status is unreadable"));

    const { container } = render(<DeepSeekKeyOnboarding />);

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("the no-key prompt names the setup step without claiming a key alone switches agents on", async () => {
    mockGet.mockResolvedValue({ configured: false, source: "none", masked: "" });

    render(<DeepSeekKeyOnboarding />);

    const prompt = await screen.findByTestId("deepseek-onboarding");
    expect(prompt).toHaveTextContent(/no deepseek key is configured yet/i);
    expect(prompt).toHaveTextContent(/open settings/i);
    expect(prompt).not.toHaveTextContent(/switch the agents on/i);
  });
});
