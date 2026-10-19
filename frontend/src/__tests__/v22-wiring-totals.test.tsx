import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { api } from "@/lib/api";
import AdsPage from "@/app/ads/page";
import TasksPage from "@/app/tasks/page";

/**
 * V22/L3 — the declared-total reads (W2 D3/D4).
 *
 * Both `/tasks` and `/ads/campaigns` emit `X-Polsia-Total` with the FULL filtered count
 * (`backend/app/api/v1/totals.py`). The pages read the body only, so a capped read said
 * "the API reports no total" / printed the page length as the total. The client now captures the
 * response header through `api.get`'s `onHeaders` hook and prints the server's own number.
 *
 * Pinned here:
 *  - `/ads` header: "50 of 137 campaigns" when the read is capped; "2 campaigns" when it is not;
 *  - `/tasks` capped board: "Showing the first 500 of 2160 tasks";
 *  - a missing header keeps the old, still-true cap language (never a fabricated total).
 */

type ReadInit = { onHeaders?: (headers: Headers) => void };

const mockApiGet = api.get as jest.MockedFunction<
  (path: string, init?: ReadInit) => Promise<unknown>
>;

function campaign(id: number): Record<string, unknown> {
  return {
    id,
    platform: "meta",
    external_id: null,
    name: `campaign ${id}`,
    goal: null,
    status: "active",
    daily_budget_usd: 1,
    total_spent_usd: 1,
    created_at: "2026-10-13T18:21:39Z",
  };
}

function task(id: number): Record<string, unknown> {
  return {
    id,
    title: `task ${id}`,
    agent_type: "social_media",
    status: "pending",
    priority: 3,
    created_at: "2026-10-13T18:21:39Z",
  };
}

function headersWithTotal(total: string | null): Headers {
  const headers = new Headers();
  if (total !== null) headers.set("X-Polsia-Total", total);
  return headers;
}

beforeEach(() => {
  mockApiGet.mockReset();
});

describe("Ads — the header count is the server's declared total (D4)", () => {
  it("a capped read prints 'shown of total campaigns', not the page length as the total", async () => {
    mockApiGet.mockImplementation((path, init) => {
      if (!path.startsWith("/ads/campaigns")) return Promise.reject(new Error(path));
      init?.onHeaders?.(headersWithTotal("137"));
      return Promise.resolve(Array.from({ length: 50 }, (_, index) => campaign(50 - index)));
    });

    render(<AdsPage />);

    expect(await screen.findByTestId("ads-count")).toHaveTextContent("50 of 137 campaigns");
  });

  it("a complete read prints the total alone", async () => {
    mockApiGet.mockImplementation((path, init) => {
      if (!path.startsWith("/ads/campaigns")) return Promise.reject(new Error(path));
      init?.onHeaders?.(headersWithTotal("2"));
      return Promise.resolve([campaign(2), campaign(1)]);
    });

    render(<AdsPage />);

    expect(await screen.findByTestId("ads-count")).toHaveTextContent("2 campaigns");
  });

  it("a missing header keeps the honest page-length count (no fabricated total)", async () => {
    mockApiGet.mockImplementation((path) => {
      if (!path.startsWith("/ads/campaigns")) return Promise.reject(new Error(path));
      return Promise.resolve([campaign(1)]);
    });

    render(<AdsPage />);

    expect(await screen.findByTestId("ads-count")).toHaveTextContent("1 campaign");
  });
});

describe("Tasks — the capped board names the server's total (D3)", () => {
  it("a full 500-row board prints 'Showing the first 500 of 2160 tasks'", async () => {
    mockApiGet.mockImplementation((path, init) => {
      if (path.startsWith("/tasks")) {
        init?.onHeaders?.(headersWithTotal("2160"));
        return Promise.resolve(Array.from({ length: 500 }, (_, index) => task(500 - index)));
      }
      return Promise.reject(new Error(path));
    });

    render(<TasksPage />);

    expect(
      await screen.findByText(/Showing the first 500 of 2160 tasks/),
    ).toBeInTheDocument();
    // The old false claim cannot survive.
    expect(screen.queryByText(/API reports no total/)).toBeNull();
  });

  it("a missing header keeps the old cap language (nothing was measured)", async () => {
    mockApiGet.mockImplementation((path) => {
      if (path.startsWith("/tasks")) {
        return Promise.resolve(Array.from({ length: 500 }, (_, index) => task(500 - index)));
      }
      return Promise.reject(new Error(path));
    });

    render(<TasksPage />);

    expect(
      await screen.findByText(/Showing the first 500 tasks/),
    ).toBeInTheDocument();
    expect(screen.getByText(/API reports no total/)).toBeInTheDocument();
  });
});

describe("Ads — Refresh re-reads the total too", () => {
  it("the second read's header replaces the first (a total is never stale)", async () => {
    let call = 0;
    mockApiGet.mockImplementation((path, init) => {
      if (!path.startsWith("/ads/campaigns")) return Promise.reject(new Error(path));
      call += 1;
      init?.onHeaders?.(headersWithTotal(call === 1 ? "60" : "61"));
      return Promise.resolve([campaign(1)]);
    });

    render(<AdsPage />);
    expect(await screen.findByTestId("ads-count")).toHaveTextContent("1 of 60 campaigns");

    await userEvent.setup().click(screen.getByTestId("ads-refresh"));

    expect(await screen.findByTestId("ads-count")).toHaveTextContent("1 of 61 campaigns");
  });
});
