import { render, screen } from "@testing-library/react";

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

    const nullCard = (await screen.findByText(nullTask.title)).closest("div.bg-gray-800");
    expect(nullCard).not.toBeNull();
    expect(nullCard).toHaveTextContent("Priority —");
    expect(nullCard).toHaveTextContent("unknown");
    expect(nullCard).not.toHaveTextContent("Priority null");

    const fineCard = screen.getByText(fineTask.title).closest("div.bg-gray-800");
    expect(fineCard).not.toBeNull();
    expect(fineCard).toHaveTextContent("Priority 3");
    expect(fineCard).toHaveTextContent("pending");
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

    const card = (await screen.findByText("fixture: post with a null status")).closest(
      "div.bg-gray-800",
    );
    expect(card).not.toBeNull();
    expect(card).toHaveTextContent("unknown");
  });

  it("outreach page renders a null status as the em dash in the status cell", async () => {
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

    const row = (await screen.findByText("null-status@example.test")).closest("tr");
    expect(row).not.toBeNull();
    const statusCell = row?.querySelectorAll("td")[3];
    expect(statusCell).toBeDefined();
    expect(statusCell).toHaveTextContent("—");
  });
});
