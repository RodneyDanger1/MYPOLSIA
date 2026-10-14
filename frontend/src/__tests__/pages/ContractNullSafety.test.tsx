import { render, screen, within } from "@testing-library/react";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api, type Task } from "@/lib/api";
import OutreachPage from "@/app/outreach/page";
import SocialPage from "@/app/social/page";
import TasksPage from "@/app/tasks/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/**
 * L1-11 null-safety pins: the backend now SERVES a schema-legal NULL row (the columns are
 * DDL-nullable — `server_default` without `NOT NULL`), so the pages must render it honestly
 * instead of crashing or printing an empty chip:
 *   * tasks:   null priority -> em dash; null status -> the gray "unknown" badge
 *   * social:  null status   -> the gray "unknown"
 *   * outreach: null status  -> the em dash in the status cell
 * Recorded values must be untouched by the fallbacks.
 */
describe("null-safe list pages (L1-11)", () => {
  afterEach(() => mockApiGet.mockReset());

  it("tasks page renders null priority/status without an empty chip", async () => {
    const nullTask: Task = {
      id: 1,
      title: "fixture: null priority/status",
      agent_type: "finance",
      status: null,
      priority: null,
      created_at: null,
    };
    const fineTask: Task = {
      id: 2,
      title: "fixture: recorded values",
      agent_type: "social_media",
      status: "pending",
      priority: 3,
      created_at: null,
    };
    mockApiGet.mockResolvedValue([nullTask, fineTask]);

    render(<TasksPage />);

    // Wave U2 (coder F1 rebuild): cards are `Open task:` buttons (was div.bg-gray-800) and the
    // card no longer prints a Priority row at all — the live board shows none (spec: tasks.md;
    // old -> new recorded in the wave receipt). The L1-11 intent is intact: a served null status
    // renders the muted "unknown" chip — never a blank one, never the word null.
    const nullCard = await screen.findByRole("button", { name: `Open task: ${nullTask.title}` });
    expect(screen.getByText("unknown")).toBeInTheDocument();
    expect(screen.getByText("finance")).toBeInTheDocument();
    expect(nullCard).not.toHaveTextContent("Priority null");

    const fineCard = screen.getByRole("button", { name: `Open task: ${fineTask.title}` });
    expect(screen.getByText("pending")).toBeInTheDocument();
    expect(screen.getByText("social_media")).toBeInTheDocument();
    expect(fineCard).toBeInTheDocument();
  });

  it("social page renders a null status as the gray 'unknown', never an empty chip", async () => {
    mockApiGet.mockResolvedValue([
      {
        id: 21,
        content: "fixture: post with a null status",
        status: null,
        published_at: null,
        engagement: null,
      },
    ]);

    render(<SocialPage />);

    // Wave U2 (coder F2 restyle): the card element is the restyled Card
    // (`data-testid="social-post"`), not the retired `div.bg-gray-800` (old -> new in the wave
    // receipt). The L1-11 pin itself is unchanged: a served null status renders the gray "unknown".
    const card = (await screen.findByText("fixture: post with a null status")).closest(
      '[data-testid="social-post"]',
    );
    expect(card).not.toBeNull();
    expect(card).toHaveTextContent("unknown");
  });

  it("outreach page renders a null status as the em dash in the status chip", async () => {
    mockApiGet.mockResolvedValue([
      {
        id: 31,
        email: "null-status@example.test",
        first_name: null,
        company: null,
        status: null,
        created_at: null,
      },
    ]);

    render(<OutreachPage />);

    // Wave U2 (coder F2 restyle): the 5-column table became live-style rows; the status lives in
    // the row's chip (`data-testid="prospect-status"`) instead of `td` index 3 (old -> new in the
    // wave receipt). The pin is unchanged: a served null status renders the em dash.
    const row = (await screen.findByText("null-status@example.test")).closest(
      '[data-testid="prospect-row"]',
    );
    expect(row).not.toBeNull();
    const statusChip = within(row as HTMLElement).getByTestId("prospect-status");
    expect(statusChip).toHaveTextContent("—");
  });
});
