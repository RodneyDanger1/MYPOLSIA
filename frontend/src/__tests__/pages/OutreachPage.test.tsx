import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api } from "@/lib/api";
import OutreachPage from "@/app/outreach/page";

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

    const email = await screen.findByText(row.email);
    const tr = email.closest("tr");
    expect(tr).not.toBeNull();
    expect(tr).toHaveTextContent("date unknown");
    expect(tr).toHaveTextContent("—");
    expect(tr).not.toHaveTextContent("Invalid Date");
  });

  it("still prints the created date when the server sends one", async () => {
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
    const tr = email.closest("tr");
    expect(tr).not.toBeNull();
    expect(tr).toHaveTextContent(new Date(datedIso).toLocaleDateString());
    expect(tr).not.toHaveTextContent("date unknown");
  });
});
