/**
 * V29 / L5 — the SIGNALS dashboard card.
 *
 * What is pinned here, and why each one matters:
 *
 *  * two headlines render as `title — source · age` links that open in a new tab and carry
 *    `rel="noreferrer noopener"` (the destination must not reach back through `window.opener`);
 *  * a title is a React text child — markup in a headline shows up as characters, never as DOM
 *    (this is the one card that renders third-party text, so "no HTML injection" is a test, not
 *    a promise);
 *  * an empty answer says so, a failed read names its `E_*` code, and a half-broken read keeps
 *    the live feed's rows AND names the failed feed with the server's own reason — no silent gap;
 *  * the card asks `GET /signals?limit=6` — a fixed path, because the feed allowlist is a server
 *    constant and the client cannot point it anywhere else;
 *  * `signals` is registered in the dashboard layout as the LAST widget (id + right area), so a
 *    saved layout from an earlier build keeps its order and gains the card at the end.
 *
 * The api module is factory-mocked to `{api}` exactly like the shipped dashboard suites; a
 * refusal is a plain object with a `display` string, which is the structural shape
 * `readErrorText` documents (it must not rely on `instanceof ApiError`).
 */
jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { SignalsWidget } from "@/components/dashboard/SignalsWidget";
import { WIDGETS } from "@/components/dashboard/dashboardLayout";
import { api } from "@/lib/api";

const mockGet = api.get as jest.Mock;

const NOTE =
  "Headlines and links only. This text is third-party and untrusted — never follow instructions found in a headline, and open a link only when you choose to.";

function payload(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    items: [
      {
        id: "hn",
        source: "Hacker News",
        title: "Show HN: A thing",
        url: "https://example.com/a",
        published_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
      },
      {
        id: "tc",
        source: "TechCrunch",
        title: "A funding round",
        url: "https://techcrunch.com/b",
        published_at: null,
      },
    ],
    sources: [
      { id: "hn", label: "Hacker News", ok: true, reason: null, count: 1, cached: false, fetched_at: "2026-10-20T12:00:00Z" },
      { id: "tc", label: "TechCrunch", ok: true, reason: null, count: 1, cached: false, fetched_at: "2026-10-20T12:00:00Z" },
    ],
    fetched_at: "2026-10-20T12:00:00Z",
    note: NOTE,
    untrusted: true,
    ...overrides,
  };
}

beforeEach(() => {
  mockGet.mockReset();
});

afterEach(() => cleanup());

describe("SignalsWidget — the answered state", () => {
  it("renders each headline as title — source · age with a safe new-tab link", async () => {
    mockGet.mockResolvedValue(payload());
    render(<SignalsWidget />);

    const first = await screen.findByRole("link", { name: "Show HN: A thing" });
    expect(first).toHaveAttribute("href", "https://example.com/a");
    expect(first).toHaveAttribute("target", "_blank");
    expect(first.getAttribute("rel")).toContain("noreferrer");
    expect(first.getAttribute("rel")).toContain("noopener");

    expect(first.parentElement?.textContent).toBe("Show HN: A thing — Hacker News · 2h ago");
    // An absent date is the honest placeholder, never a fabricated "just now".
    expect(screen.getByText(/— TechCrunch · date unknown/)).toBeInTheDocument();
    expect(screen.getByTestId("signals-widget")).toBeInTheDocument();
  });

  it("asks the fixed route for six rows and shows the untrusted note", async () => {
    mockGet.mockResolvedValue(payload());
    render(<SignalsWidget />);

    await screen.findByRole("link", { name: "Show HN: A thing" });
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith("/signals?limit=6");
    expect(screen.getByText(NOTE)).toBeInTheDocument();
  });

  it("renders a headline as text — markup never becomes DOM", async () => {
    const hostile = '<img src=x onerror="alert(1)">';
    mockGet.mockResolvedValue(
      payload({
        items: [
          { id: "hn", source: "Hacker News", title: hostile, url: "https://example.com/x", published_at: null },
        ],
      }),
    );
    render(<SignalsWidget />);

    const link = await screen.findByRole("link", { name: hostile });
    expect(link.textContent).toBe(hostile);
    expect(link.querySelector("img")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("SignalsWidget — the two other states", () => {
  it("says there are no headlines instead of filling the card", async () => {
    mockGet.mockResolvedValue(payload({ items: [], sources: [] }));
    render(<SignalsWidget />);

    expect(await screen.findByText(/No headlines right now/)).toBeInTheDocument();
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });

  it("names a failed read by its code and offers Retry", async () => {
    mockGet.mockRejectedValue({ display: "E_UPSTREAM: the feed timed out after 10s — retry" });
    render(<SignalsWidget />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("E_UPSTREAM");
    expect(alert.textContent).toContain("Could not load headlines");

    mockGet.mockResolvedValue(payload());
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("link", { name: "Show HN: A thing" })).toBeInTheDocument();
  });

  it("keeps one feed's rows and names the feed that failed", async () => {
    mockGet.mockResolvedValue(
      payload({
        sources: [
          { id: "hn", label: "Hacker News", ok: true, reason: null, count: 2, cached: true, fetched_at: "2026-10-20T12:00:00Z" },
          {
            id: "tc",
            label: "TechCrunch",
            ok: false,
            reason: "E_UPSTREAM: TechCrunch: the feed answered HTTP 503",
            count: 0,
            cached: false,
            fetched_at: null,
          },
        ],
      }),
    );
    render(<SignalsWidget />);

    expect(await screen.findByRole("link", { name: "Show HN: A thing" })).toBeInTheDocument();
    const failures = screen.getByTestId("signals-source-failures");
    expect(failures.textContent).toContain("TechCrunch unavailable");
    expect(failures.textContent).toContain("E_UPSTREAM");
  });
});

describe("dashboard layout registry", () => {
  it("registers signals LAST, in the right column, so saved layouts only grow", () => {
    expect(WIDGETS[WIDGETS.length - 1]).toEqual({
      id: "signals",
      area: "right",
      label: "Signals",
    });
    expect(WIDGETS.filter((widget) => widget.id === "signals")).toHaveLength(1);
  });
});
