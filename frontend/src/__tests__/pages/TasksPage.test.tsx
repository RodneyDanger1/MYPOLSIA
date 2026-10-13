import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api, type Task } from "@/lib/api";
import TasksPage from "@/app/tasks/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/**
 * The fixture is declared as `Task[]` on purpose. `Task.created_at` is `datetime | None` on the
 * server (`TimestampedORM.created_at`, `app/schemas/common.py`) — the tasks table's column is
 * nullable — so `created_at: null` below is a real wire value. Under the old strict
 * `created_at: string` this file does not compile, which is the contract these tests pin.
 */
const wireTask: Task = {
  id: 7,
  title: "fixture: task without a timestamp",
  agent_type: "social_media",
  status: "pending",
  priority: 3,
  created_at: null,
};

const datedIso = "2026-10-05T12:30:00Z";

describe("TasksPage", () => {
  afterEach(() => mockApiGet.mockReset());

  it("renders a null created_at as the placeholder, never as an invalid date", async () => {
    mockApiGet.mockResolvedValue([wireTask]);

    render(<TasksPage />);

    expect(await screen.findByText(wireTask.title)).toBeInTheDocument();
    expect(screen.getByText("date unknown")).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });

  it("still prints the timestamp when the server sends one", async () => {
    mockApiGet.mockResolvedValue([{ ...wireTask, id: 8, title: "fixture: task with a timestamp", created_at: datedIso }]);

    render(<TasksPage />);

    expect(await screen.findByText("fixture: task with a timestamp")).toBeInTheDocument();
    expect(screen.getByText(new Date(datedIso).toLocaleString())).toBeInTheDocument();
    expect(screen.queryByText("date unknown")).toBeNull();
  });
});
