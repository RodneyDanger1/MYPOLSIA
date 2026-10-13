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

/**
 * T15e: the chip map must recognise the backend's full documented status set — including
 * `rejected` (D2's refused-before-execution status landing in this wave's T03) and the
 * cancelled/awaiting states that were already on the wire. An unrecognised word keeps the gray
 * fallback, so both halves (complete map, intact fallback) stay pinned.
 */
describe("TasksPage status chips (T15e)", () => {
  afterEach(() => mockApiGet.mockReset());

  const recognized: Array<[string, string]> = [
    ["cancelled", "text-gray-300"],
    ["awaiting_approval", "text-orange-400"],
    ["rejected", "text-rose-400"],
  ];

  it.each(recognized)("styles %s with its own chip, not the unknown fallback", async (status, cls) => {
    mockApiGet.mockResolvedValue([{ ...wireTask, id: 100, title: `fixture: ${status}`, status }]);

    render(<TasksPage />);

    const card = (await screen.findByText(`fixture: ${status}`)).closest("div.bg-gray-800");
    const chip = card?.querySelector("span");
    expect(chip).not.toBeNull();
    expect(chip).toHaveTextContent(status);
    expect(chip?.className).toContain(cls);
    expect(chip?.className).not.toContain("bg-gray-700");
  });

  it("an unrecognised status still gets the gray fallback chip", async () => {
    mockApiGet.mockResolvedValue([
      { ...wireTask, id: 101, title: "fixture: unknown status", status: "weird_future_status" },
    ]);

    render(<TasksPage />);

    const card = (await screen.findByText("fixture: unknown status")).closest("div.bg-gray-800");
    const chip = card?.querySelector("span");
    expect(chip).toHaveTextContent("weird_future_status");
    expect(chip?.className).toContain("bg-gray-700");
  });
});

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
