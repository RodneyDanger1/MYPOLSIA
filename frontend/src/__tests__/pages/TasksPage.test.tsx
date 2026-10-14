import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api, type Task } from "@/lib/api";
import { absoluteStamp, relativeTime } from "@/components/dashboard/format";
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
 * `rejected` (D2's refused-before-execution status) and the cancelled/awaiting states that were
 * already on the wire. An unrecognised word keeps the muted fallback, so both halves (complete
 * map, intact fallback) stay pinned.
 *
 * Wave U2 (coder F1 rebuild): cards are `Open task:` buttons and the chip hues moved to the
 * 800-step palette on the light canvas (was text-gray-300 / text-orange-400 / text-rose-400 with
 * a bg-gray-700 fallback — old -> new recorded in the wave receipt). The pin's INTENT is
 * unchanged: every documented status reads its own chip; an unknown word keeps the fallback.
 */
describe("TasksPage status chips (T15e)", () => {
  afterEach(() => mockApiGet.mockReset());

  const recognized: Array<[string, string]> = [
    ["cancelled", "text-neutral-600"],
    ["awaiting_approval", "text-orange-800"],
    ["rejected", "text-rose-800"],
  ];

  it.each(recognized)("styles %s with its own chip, not the unknown fallback", async (status, cls) => {
    mockApiGet.mockResolvedValue([{ ...wireTask, id: 100, title: `fixture: ${status}`, status }]);

    render(<TasksPage />);

    const card = await screen.findByRole("button", { name: `Open task: fixture: ${status}` });
    const chip = within(card).getByText(status);
    expect(chip).toBeInTheDocument();
    expect(chip.className).toContain(cls);
    expect(chip.className).not.toContain("text-ink-muted");
  });

  it("an unrecognised status still gets the muted fallback chip", async () => {
    mockApiGet.mockResolvedValue([
      { ...wireTask, id: 101, title: "fixture: unknown status", status: "weird_future_status" },
    ]);

    render(<TasksPage />);

    const card = await screen.findByRole("button", { name: "Open task: fixture: unknown status" });
    const chip = within(card).getByText("weird_future_status");
    expect(chip).toBeInTheDocument();
    expect(chip.className).toContain("text-ink-muted");
  });
});

describe("TasksPage", () => {
  afterEach(() => mockApiGet.mockReset());

  it("renders a null created_at as the placeholder, never as an invalid date", async () => {
    mockApiGet.mockResolvedValue([wireTask]);

    render(<TasksPage />);

    const card = await screen.findByRole("button", { name: `Open task: ${wireTask.title}` });
    expect(card).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();

    // Wave U2: a non-completed card prints no date by design (cap-05's age chip is DONE-only);
    // the placeholder is pinned where it now lives — the detail sheet (old -> new in the wave
    // receipt). The intent is unchanged: null must never render as "Invalid Date".
    await userEvent.click(card);
    expect((await screen.findAllByText("date unknown")).length).toBeGreaterThan(0);
  });

  it("prints the real timestamp when the server sends one", async () => {
    mockApiGet.mockResolvedValue([
      {
        ...wireTask,
        id: 8,
        title: "fixture: task with a timestamp",
        status: "completed",
        created_at: datedIso,
      },
    ]);

    render(<TasksPage />);

    await screen.findByRole("button", { name: "Open task: fixture: task with a timestamp" });
    // Wave U2: the DONE card shows the relative age with the absolute stamp in its title
    // (format.ts treatment, same as the dashboard/outreach lists), and with no completed_at on
    // the wire it labels the created age honestly as "created ..." (old -> new in the wave
    // receipt; the old pin was toLocaleString() on the card).
    expect(screen.getByText(`created ${relativeTime(datedIso)}`)).toBeInTheDocument();
    expect(screen.getByTitle(`created ${absoluteStamp(datedIso)}`)).toBeInTheDocument();
  });
});
