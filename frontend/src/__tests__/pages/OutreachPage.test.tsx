import { render, screen, within } from "@testing-library/react";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api } from "@/lib/api";
import OutreachPage from "@/app/outreach/page";
import { absoluteStamp, relativeTime } from "@/components/dashboard/format";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/** The page's own `Prospect` wire shape: `created_at` is `datetime | None` server-side
 *  (`ProspectResponse.created_at`, `app/schemas/social.py`), and so are `first_name`/`company`. */
type WireProspect = {
  id: number;
  email: string;
  first_name: string | null;
  company: string | null;
  status: string;
  created_at: string | null;
};

const datedIso = "2026-10-05T12:30:00Z";

describe("OutreachPage", () => {
  afterEach(() => mockApiGet.mockReset());

  it("renders a null created_at as the placeholder and null name/company as the em dash", async () => {
    const row: WireProspect = {
      id: 11,
      email: "missing-details@example.test",
      first_name: null,
      company: null,
      status: "new",
      created_at: null,
    };
    mockApiGet.mockResolvedValue([row]);

    render(<OutreachPage />);

    // Wave U2 (coder F2 restyle): the 5-column table became live-style inset rows
    // (`data-testid="prospect-row"`); old `closest("tr")` -> new selector recorded in the wave
    // receipt. The assertions (placeholders, no Invalid Date) are the original pins.
    const email = await screen.findByText(row.email);
    const listRow = email.closest('[data-testid="prospect-row"]');
    expect(listRow).not.toBeNull();
    expect(listRow).toHaveTextContent("date unknown");
    expect(listRow).toHaveTextContent("—");
    expect(listRow).not.toHaveTextContent("Invalid Date");
  });

  it("shows the created age relatively, with the exact stamp on the row's title attribute", async () => {
    const row: WireProspect = {
      id: 12,
      email: "dated-row@example.test",
      first_name: "Fixture",
      company: "E2E Fixtures Co",
      status: "contacted",
      created_at: datedIso,
    };
    mockApiGet.mockResolvedValue([row]);

    render(<OutreachPage />);

    const email = await screen.findByText(row.email);
    const listRow = email.closest('[data-testid="prospect-row"]');
    expect(listRow).not.toBeNull();
    // Wave U2 DELIBERATE CHANGE (outreach.md R3: live-family relative age like `2d ago`, exact
    // stamp on hover). OLD assertion: `toHaveTextContent(new Date(datedIso).toLocaleDateString())`
    // — the locale-dependent absolute date the GAP-MAP §10 drift flagged. NEW: the relative form
    // plus the pinned `absoluteStamp` on the title (recorded old -> new in the wave receipt).
    const age = within(listRow as HTMLElement).getByTestId("prospect-age");
    expect(age).toHaveTextContent(relativeTime(datedIso));
    expect(age).toHaveAttribute("title", absoluteStamp(datedIso));
    expect(listRow).not.toHaveTextContent("date unknown");
  });
});
