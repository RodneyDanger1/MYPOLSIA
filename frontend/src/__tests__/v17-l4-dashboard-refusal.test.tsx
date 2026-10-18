/**
 * WAVE V17 / seat L4 — dashboard regions must render a refusal BY NAME.
 *
 * Every dashboard panel used `e instanceof Error ? e.message : String(e)` for its error state, which
 * keeps the server's sentence but DROPS the refusal code and the `how_to_fix` hint that
 * `ApiError.display` carries (the law every page-level hook already follows: useInvoices,
 * useSchedules, useFinanceRead, useAdsRead). A reader on the dashboard therefore saw
 * "sandbox is on" with no `E_SANDBOX_BLOCKED`, while the same refusal on /invoices named itself.
 *
 * The fix is one structural helper (`readErrorText` in components/dashboard/states.tsx — structural
 * so suites that mock @/lib/api down to `{api}` do not trip over `instanceof ApiError`) used by every
 * panel and by `useAgentStatus` (the Agents page + the dashboard's agent grid).
 *
 * RED leg: pre-fix the helper does not exist and the panels render the code-less message.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { render, screen } from "@testing-library/react";

import { readErrorText } from "@/components/dashboard/states";
import { CapsPanel } from "@/components/dashboard/CapsPanel";
import { SocialWidget } from "@/components/dashboard/SocialWidget";
import { ApiError, api } from "@/lib/api";

const mockGet = api.get as jest.Mock;

const REFUSAL = new ApiError(
  403,
  "sandbox mode is on — external sends are blocked",
  "E_SANDBOX_BLOCKED",
  "turn sandbox off in Settings to send for real",
);

beforeEach(() => {
  mockGet.mockReset();
});

describe("readErrorText — the refusal, by name", () => {
  it("renders ApiError.display (code + message + how_to_fix)", () => {
    expect(readErrorText(REFUSAL)).toBe(
      "E_SANDBOX_BLOCKED: sandbox mode is on — external sends are blocked — turn sandbox off in Settings to send for real",
    );
  });

  it("keeps a plain Error's own message and stringifies a non-Error throw", () => {
    expect(readErrorText(new Error("network down"))).toBe("network down");
    expect(readErrorText("boom")).toBe("boom");
  });
});

describe("dashboard panels — a failed read is NAMED, code included", () => {
  it("CapsPanel shows the refusal code and the fix hint, not just the message", async () => {
    mockGet.mockImplementation((path: string) =>
      path === "/channels/caps" ? Promise.reject(REFUSAL) : Promise.reject(new Error(`unexpected read: ${path}`)),
    );
    render(<CapsPanel />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("E_SANDBOX_BLOCKED");
    expect(alert.textContent).toContain("turn sandbox off in Settings");
  });

  it("SocialWidget shows the refusal code too (one shared helper, no drift)", async () => {
    mockGet.mockImplementation(() => Promise.reject(REFUSAL));
    render(<SocialWidget />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("E_SANDBOX_BLOCKED");
  });
});
