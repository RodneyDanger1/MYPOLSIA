import { act, fireEvent, render, screen } from "@testing-library/react";

jest.mock("next/link", () => {
  return function MockLink({
    href,
    children,
    className,
  }: {
    href: string;
    children: React.ReactNode;
    className?: string;
  }) {
    return (
      <a href={href} className={className}>
        {children}
      </a>
    );
  };
});

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api } from "@/lib/api";
import {
  DeepSeekKeyOnboarding,
  ONBOARDING_DISMISS_KEY,
} from "@/components/dashboard/DeepSeekKeyOnboarding";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

const NOT_CONFIGURED = { configured: false, source: "none" as const, masked: "…" };
const CONFIGURED = { configured: true, source: "stored" as const, masked: "sk-…1234" };

describe("DeepSeekKeyOnboarding", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    window.localStorage.clear();
  });

  it("shows the onboarding prompt while no key is configured, with a link to Settings", async () => {
    mockApiGet.mockResolvedValue(NOT_CONFIGURED);

    render(<DeepSeekKeyOnboarding />);

    const banner = await screen.findByTestId("deepseek-onboarding");
    expect(banner).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /Open Settings/ });
    expect(link).toHaveAttribute("href", "/settings");
  });

  it("auto-hides once a key is configured", async () => {
    mockApiGet.mockResolvedValue(CONFIGURED);

    render(<DeepSeekKeyOnboarding />);
    // Deterministic flush: the status fetch's promise and every React update it schedules
    // are settled before the assertion, so this test can actually catch a banner that
    // wrongly renders for a configured app (it does — see the planted-defect RED leg).
    await act(async () => {});

    expect(screen.queryByTestId("deepseek-onboarding")).toBeNull();
    expect(screen.queryByRole("link", { name: /Open Settings/ })).toBeNull();
  });

  it("stays hidden — and does not nag — when the status read fails", async () => {
    mockApiGet.mockRejectedValue(new Error("E_NETWORK: timeout"));

    render(<DeepSeekKeyOnboarding />);
    await act(async () => {});

    expect(screen.queryByTestId("deepseek-onboarding")).toBeNull();
  });

  it("dismisses the prompt and remembers the dismissal for this browser", async () => {
    mockApiGet.mockResolvedValue(NOT_CONFIGURED);

    const first = render(<DeepSeekKeyOnboarding />);
    expect(await screen.findByTestId("deepseek-onboarding")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByTestId("deepseek-onboarding")).toBeNull();
    expect(window.localStorage.getItem(ONBOARDING_DISMISS_KEY)).toBe("1");

    // A fresh mount (next visit) stays dismissed — persistence, not just local state.
    first.unmount();
    render(<DeepSeekKeyOnboarding />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByTestId("deepseek-onboarding")).toBeNull();
  });

  it("writes no key material to client-side storage, only the dismissal flag", async () => {
    mockApiGet.mockResolvedValue(NOT_CONFIGURED);

    render(<DeepSeekKeyOnboarding />);
    fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));

    expect(window.localStorage.length).toBe(1);
    expect(window.localStorage.key(0)).toBe(ONBOARDING_DISMISS_KEY);
    expect(window.localStorage.getItem(ONBOARDING_DISMISS_KEY)).toBe("1");
    expect(window.sessionStorage.length).toBe(0);
  });
});
