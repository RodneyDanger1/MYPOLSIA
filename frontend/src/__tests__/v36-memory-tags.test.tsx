/**
 * WAVE 8 / F-42 — the memory screen renders the row's own stored TAGS, bounded and honestly.
 *
 * THE DEFECT (visuals `VISUALS-FINDINGS-W6/W7.md`; re-measured live on `dc78e80` 2026-10-29):
 * `GET /memory?limit=50` (the LIST branch) carried tags on **50/50** rows, but `/memory` only ever
 * issued the SEMANTIC read (`?q=`), whose hits are built from Chroma metadata and carry **no**
 * `tags` field at all — so the screen rendered zero tag strings while the wire had them.
 *
 * THE FIX UNDER TEST: an explicit, click-only "Recent entries" read of the LIST branch (the page's
 * read-on-request law is unchanged) renders each row's tags — up to 6 chips plus "+N more" with the
 * full list in the title — and states the branch's own totals provenance (X-Polsia-Total); a search
 * hit that carries no tags claims nothing about them.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import MemoryPage, { type Memory } from "@/app/memory/page";
import {
  MEMORY_TAGS_SHOWN,
  hiddenTagCount,
  memoryTags,
  shownTags,
} from "@/components/memory/tags";

const mockGet = api.get as jest.Mock;

type HeadersLike = { get: (name: string) => string | null };

/** Serve the browse read with a declared total (the header and the body from ONE response). */
function serveBrowse(rows: Memory[], total: string | null) {
  mockGet.mockImplementation((path: string, init?: { onHeaders?: (headers: HeadersLike) => void }) => {
    if (path === "/memory?limit=20") {
      init?.onHeaders?.({ get: (name: string) => (name === "X-Polsia-Total" ? total : null) });
      return Promise.resolve(rows);
    }
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

function serveSearch(rows: Memory[]) {
  mockGet.mockImplementation((path: string) => {
    if (path.startsWith("/memory?q=")) return Promise.resolve(rows);
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

const ROW: Memory = {
  id: 225,
  category: "agent_run",
  title: "social_media: draft a tweet",
  content: "the content is always present on the wire",
  created_at: null,
  fallback: null,
  tags: ["social_media", "company_id:1", "company_name:Cinder & Lye", "claim:banned-current", "claim:reviewed:2026-10-29"],
};

beforeEach(() => {
  mockGet.mockReset();
});

describe("F-42 — Recent entries renders the stored tags", () => {
  it("fires nothing on mount, then reads the LIST branch and renders each tag", async () => {
    serveBrowse([ROW, { ...ROW, id: 222, title: "email_outreach: drill", tags: ["email_outreach"] }], "221");
    render(<MemoryPage />);

    // The page's read-on-request law is intact.
    expect(mockGet).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("memory-browse"));

    expect(await screen.findByText("social_media: draft a tweet")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet.mock.calls[0][0]).toBe("/memory?limit=20");

    const tagged = screen.getAllByTestId("memory-tags");
    expect(tagged).toHaveLength(2);
    for (const tag of ROW.tags ?? []) {
      expect(within(tagged[0]).getByText(tag)).toBeInTheDocument();
    }
    // The list branch's own declared total is stated as such.
    const total = screen.getByTestId("memory-browse-total");
    expect(total).toHaveTextContent(/Showing the newest 2 of 221 stored entries/i);
    expect(total).toHaveTextContent(/219 older entries are not shown/i);
    // Each chip keeps the full tag for hover/copy — the TEXT is never altered.
    expect(within(tagged[0]).getByText("claim:reviewed:2026-10-29")).toHaveAttribute(
      "title",
      "claim:reviewed:2026-10-29",
    );
  });

  it("bounds the display at six chips plus a '+N more' whose title lists the rest", async () => {
    const tags = Array.from({ length: 8 }, (_, i) => `claim:tag-${i + 1}`);
    serveBrowse([{ ...ROW, tags }], "1");
    render(<MemoryPage />);
    fireEvent.click(screen.getByTestId("memory-browse"));

    const group = await screen.findByTestId("memory-tags");
    expect(within(group).getAllByTestId("memory-tag")).toHaveLength(MEMORY_TAGS_SHOWN);
    const more = within(group).getByTestId("memory-tags-more");
    expect(more).toHaveTextContent("+2 more");
    expect(more).toHaveAttribute("title", "claim:tag-7, claim:tag-8");
  });

  it("drops junk tag shapes and duplicates instead of rendering them", async () => {
    serveBrowse(
      [
        {
          ...ROW,
          tags: [1, null, " ", "alpha", "alpha", " beta ", true] as unknown as string[],
        },
      ],
      "1",
    );
    render(<MemoryPage />);
    fireEvent.click(screen.getByTestId("memory-browse"));

    const group = await screen.findByTestId("memory-tags");
    const chips = within(group).getAllByTestId("memory-tag");
    expect(chips.map((chip) => chip.textContent)).toEqual(["alpha", "beta"]);
    expect(within(group).queryByTestId("memory-tags-more")).toBeNull();
  });

  it("a row with no tags renders NO tag claim (absence is not 'no tags')", async () => {
    serveBrowse([{ ...ROW, tags: null }, { ...ROW, id: 224, tags: undefined }], "2");
    render(<MemoryPage />);
    fireEvent.click(screen.getByTestId("memory-browse"));

    await screen.findAllByText("social_media: draft a tweet");
    expect(screen.queryByTestId("memory-tags")).toBeNull();
    // No ROW claims anything about tags either (the page-level note explains the two branches).
    for (const row of screen.getAllByRole("listitem")) {
      expect(row.textContent ?? "").not.toMatch(/no tags/i);
    }
  });

  it("keeps the Forget control on a browse row (a real, addressable id)", async () => {
    serveBrowse([ROW], "1");
    render(<MemoryPage />);
    fireEvent.click(screen.getByTestId("memory-browse"));
    const group = await screen.findByTestId("memory-tags");
    const row = group.closest("li");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByRole("button", { name: "Forget" })).toBeEnabled();
  });
});

describe("F-42 — the two reads stay distinct and honest", () => {
  it("a semantic search still asks for exactly its warm read and renders no tag claim", async () => {
    serveSearch([{ ...ROW, id: "memory-225-abc", tags: undefined }]);
    render(<MemoryPage />);
    await userEvent.setup().type(screen.getByLabelText("Search company memory"), "candle");
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("social_media: draft a tweet")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet.mock.calls[0][0]).toBe("/memory?q=candle&limit=20");
    expect(mockGet.mock.calls[0][1]).toEqual({ timeoutMs: 120000 });
    expect(screen.queryByTestId("memory-tags")).toBeNull();
    // The browse disclosure never renders for a search answer.
    expect(screen.queryByTestId("memory-browse-total")).toBeNull();
    expect(screen.queryByTestId("memory-browse-no-total")).toBeNull();
  });

  it("names a failed browse read and never renders it as 'nothing stored'", async () => {
    mockGet.mockRejectedValue(
      new ApiError(500, "the memory table is unreadable", "E_DB", "retry once the database is up"),
    );
    render(<MemoryPage />);
    fireEvent.click(screen.getByTestId("memory-browse"));

    const alert = await screen.findByTestId("memory-browse-error");
    expect(alert).toHaveRole("alert");
    expect(alert).toHaveTextContent("Could not load recent entries");
    expect(alert).toHaveTextContent("E_DB");
    expect(alert).toHaveTextContent("retry once the database is up");
    expect(screen.queryByTestId("memory-browse-empty")).toBeNull();
    expect(screen.queryByText(/No entries matched/)).toBeNull();
  });

  it("says what a full page of browse results proves when the header is absent", async () => {
    const rows = Array.from({ length: 20 }, (_, i) => ({ ...ROW, id: i + 1, tags: [] }));
    serveBrowse(rows, null);
    render(<MemoryPage />);
    fireEvent.click(screen.getByTestId("memory-browse"));

    const note = await screen.findByTestId("memory-browse-no-total");
    expect(note).toHaveTextContent(/Showing the newest 20 entries/i);
    expect(note).toHaveTextContent(/declared no total, so more may exist/i);
  });

  it("prints the browse empty state only for a successful empty read", async () => {
    serveBrowse([], "0");
    render(<MemoryPage />);
    fireEvent.click(screen.getByTestId("memory-browse"));

    expect(await screen.findByTestId("memory-browse-empty")).toHaveTextContent(
      /No memory entries are stored yet/,
    );
    expect(screen.queryByText(/No entries matched that search/)).toBeNull();
  });

  it("a browse answer clears a previous search failure instead of stacking alerts", async () => {
    mockGet.mockRejectedValueOnce(
      new ApiError(503, "vector store unavailable", "E_DB", "retry once the index is back"),
    );
    serveBrowse([ROW], "1");
    render(<MemoryPage />);
    await userEvent.setup().type(screen.getByLabelText("Search company memory"), "alpha");
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByTestId("memory-search-retry");

    fireEvent.click(screen.getByTestId("memory-browse"));
    await screen.findByText("social_media: draft a tweet");
    await waitFor(() => expect(screen.queryByText("Search failed")).toBeNull());
  });

  it("the browse note states where tags come from (the measured branch rule)", () => {
    render(<MemoryPage />);
    const note = screen.getByTestId("memory-browse-note");
    expect(note).toHaveTextContent(/Recent entries reads the stored rows themselves/i);
    expect(note).toHaveTextContent(/semantic search above answers from the memory index/i);
    expect(note).toHaveTextContent(/whose hits carry no tags on this server/i);
  });
});

describe("F-42 — the tag narrowing (pure units)", () => {
  it("keeps non-blank strings, trims, dedupes, preserves order", () => {
    expect(memoryTags(["a", " b ", "a", "", "  ", 3, null, { x: 1 }])).toEqual(["a", "b"]);
    expect(memoryTags(null)).toEqual([]);
    expect(memoryTags("a,b")).toEqual([]);
    expect(memoryTags(undefined)).toEqual([]);
  });

  it("bounds by the documented count", () => {
    const tags = Array.from({ length: 9 }, (_, i) => `t${i}`);
    expect(shownTags(tags)).toHaveLength(MEMORY_TAGS_SHOWN);
    expect(hiddenTagCount(tags)).toBe(9 - MEMORY_TAGS_SHOWN);
    expect(hiddenTagCount(["a"])).toBe(0);
    expect(MEMORY_TAGS_SHOWN).toBe(6);
  });
});
