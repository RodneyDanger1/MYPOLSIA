import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api } from "@/lib/api";
import MemoryPage from "@/app/memory/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/**
 * L1-02's frontend half: the service tags a degraded read (`fallback: "sql_like"` when the vector
 * store is down and the read became a keyword scan; `"sql_unfiltered"` when the category filter was
 * dropped). The marker now reaches the wire (`MemoryItem.fallback`), so the page must DISCLOSE it —
 * a keyword scan must not render like a semantic match. An unknown marker is surfaced verbatim
 * (the contract is an open string); a semantic hit (absent/null marker) shows no badge at all.
 */
async function searchFor(query: string) {
  const user = userEvent.setup();
  render(<MemoryPage />);
  await user.type(screen.getByPlaceholderText("Search company memory…"), query);
  await user.click(screen.getByRole("button", { name: "Search" }));
}

describe("MemoryPage fallback badge (L1-02)", () => {
  afterEach(() => mockApiGet.mockReset());

  it("labels a keyword scan as 'index unavailable'", async () => {
    mockApiGet.mockResolvedValue([
      { id: 1, category: "strategy", title: "alpha plan", content: "alpha content", created_at: null, fallback: "sql_like" },
    ]);

    await searchFor("alpha");

    expect(await screen.findByText("alpha plan")).toBeInTheDocument();
    expect(screen.getByText("keyword match — memory index unavailable")).toBeInTheDocument();
  });

  it("labels a dropped category filter distinctly", async () => {
    mockApiGet.mockResolvedValue([
      { id: 1, category: "strategy", title: "alpha plan", content: "alpha content", created_at: null, fallback: "sql_unfiltered" },
    ]);

    await searchFor("alpha");

    expect(await screen.findByText("alpha plan")).toBeInTheDocument();
    expect(screen.getByText("keyword match — category filter dropped")).toBeInTheDocument();
  });

  it("surfaces an unknown marker instead of hiding it", async () => {
    mockApiGet.mockResolvedValue([
      { id: 1, category: "strategy", title: "alpha plan", content: "alpha content", created_at: null, fallback: "sql_someday" },
    ]);

    await searchFor("alpha");

    expect(await screen.findByText("alpha plan")).toBeInTheDocument();
    expect(screen.getByText("degraded result (sql_someday)")).toBeInTheDocument();
  });

  it("shows no degradation badge for a real similarity hit", async () => {
    mockApiGet.mockResolvedValue([
      { id: "chroma-1", category: "strategy", title: "alpha plan", content: "alpha content", created_at: null, fallback: null, distance: 0.11 },
    ]);

    await searchFor("alpha");

    expect(await screen.findByText("alpha plan")).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });
});
