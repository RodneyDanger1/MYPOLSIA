import { readFileSync } from "fs";
import path from "path";

import { act, render, screen, waitFor } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { SandboxBanner } from "@/components/system/SandboxBanner";

/**
 * Wave V11 unit 2 — seat W6's suite for the app-wide SandboxBanner (capability audit B1/B2).
 *
 * Scope discipline: run with `node node_modules/jest/bin/jest.js --ci u11-sandbox-banner`.
 *
 * What is pinned here:
 *  - true renders the plain-language banner off the REAL GET /ops/sandbox read, with the flip
 *    instructions (.env + restart) so the owner is told WHY external actions are drafted;
 *  - false renders NOTHING (live is the unremarkable state), and a body that is not literally
 *    true is treated as not-true, never as a sandbox claim;
 *  - a FAILED read renders NOTHING: silence is the honest fallback, and the banner may never look
 *    like an app failure (no alert, no error text anywhere in its container);
 *  - one read per mount, nothing polled;
 *  - layout.tsx carries exactly one mount plus its import (the ONE additive insertion).
 *
 * The silence assertions use the same flush path as the true-case render (which is the positive
 * control: updates DO land in this harness), so "nothing rendered" is a settled observation.
 */

const mockApiGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;

/** Flush the banner's read microtasks so the settle happened before silence is asserted. */
async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  mockApiGet.mockReset();
});

describe("SandboxBanner — says it plainly when ON, is silent otherwise", () => {
  it("renders the persistent banner when GET /ops/sandbox says true", async () => {
    mockApiGet.mockResolvedValue({ sandbox_mode: true });

    render(<SandboxBanner />);

    const banner = await screen.findByTestId("sandbox-banner");
    expect(mockApiGet).toHaveBeenCalledWith("/ops/sandbox");
    expect(mockApiGet).toHaveBeenCalledTimes(1); // one read, nothing polled

    expect(banner).toHaveAttribute("role", "status");
    expect(banner).toHaveTextContent("Sandbox mode ON");
    const copy = screen.getByTestId("sandbox-banner-copy");
    expect(copy).toHaveTextContent("External actions are drafted, not sent");
    expect(copy).toHaveTextContent("SANDBOX_MODE");
    expect(copy).toHaveTextContent(".env");
    expect(copy).toHaveTextContent("restart");
  });

  it("renders NOTHING when the flag is false", async () => {
    mockApiGet.mockResolvedValue({ sandbox_mode: false });

    const { container } = render(<SandboxBanner />);

    await waitFor(() => expect(mockApiGet).toHaveBeenCalledWith("/ops/sandbox"));
    await settle();
    expect(container.innerHTML).toBe("");
  });

  it("a failed read renders NOTHING — never an error that looks like an app failure", async () => {
    mockApiGet.mockRejectedValue(
      new ApiError(503, "database unavailable", "E_DB", "check postgres"),
    );

    const { container } = render(<SandboxBanner />);

    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(1));
    await settle();
    expect(container.innerHTML).toBe("");
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("bodies that are not literally true are not a sandbox claim", async () => {
    for (const body of [{}, { sandbox_mode: 1 }, { sandbox_mode: "true" }, null]) {
      mockApiGet.mockReset();
      mockApiGet.mockResolvedValue(body);

      const { container, unmount } = render(<SandboxBanner />);

      await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(1));
      await settle();
      expect(container.innerHTML).toBe("");
      unmount();
    }
  });
});

describe("SandboxBanner — the app-wide mount", () => {
  it("layout.tsx carries exactly one <SandboxBanner /> mount and its import", () => {
    const layout = readFileSync(path.resolve(__dirname, "..", "app", "layout.tsx"), "utf8");
    expect(layout).toContain('import { SandboxBanner } from "@/components/system/SandboxBanner";');
    expect(layout.match(/<SandboxBanner \/>/g)).toHaveLength(1);
  });
});
