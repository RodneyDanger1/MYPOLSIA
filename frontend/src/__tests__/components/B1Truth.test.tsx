/**
 * WAVE B1 / SEAT D5 — UI + E2E TRUTH: the browser must agree with the backend.
 *
 * Three carried defects, each measured from the COMPONENT side, plus the one e2e gap that cannot
 * be measured here (labeled UNRUN in the receipt, never faked):
 *
 *   F-40 / A2A3-01  every dialog must close on Escape when focus has LEFT the panel, and Tab must
 *                   wrap. A3 moved six of the seven dialogs onto `useDialogFocusTrap`; the
 *                   CommandPalette kept its own pair (window Escape + a PANEL-scoped `onKeyDown`
 *                   Tab trap). Its Escape survives focus on `<body>`; its Tab trap does not — a
 *                   React handler on the panel never sees a key whose target is the document.
 *                   The table below drives ALL SEVEN surfaces through the same four assertions,
 *                   so the one dialog that is not on the hook cannot hide.
 *   F-37           `PhoneCard` renders `configured`/`available` honestly: `available:false` is
 *                   the unavailable state; a FAILED config read (503 included) is the failed
 *                   state, named by its code, with NO console error and NO guess at
 *                   `/phone/summary`.
 *   F-36           the task detail renders "Planned by" from the DECLARED `PlanEdge` wire type and
 *                   invents nothing when the server has no `plan` key (D1 has not landed the
 *                   backend computed field, so the live leg is BLOCKED, not simulated).
 *
 * The dialog inventory (grep `role="dialog"` over `frontend/src/components`, seven files):
 *   TaskDetailSheet · NewTaskDialog · DocumentsOverlay · DashboardEditorSheet · ActivityStrip ·
 *   PhoneMedia (MediaViewer) · CommandPalette.
 * Not modals, excluded on purpose: the DeepSeek key's inline `role="group"` confirm row, the
 * AgentTools disclosure, PhoneCodesPanel's Escape-to-re-mask (its own verb, not a modal law), and
 * `usePopover` (Escape is already bound on `document`).
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

const mockRouter = { push: jest.fn(), replace: jest.fn(), refresh: jest.fn() };

jest.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
  usePathname: () => "/dashboard",
  useSearchParams: () => new URLSearchParams(),
}));

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink({
    href,
    children,
    ...rest
  }: { href: string; children: React.ReactNode } & Record<string, unknown>) {
    return (
      <a href={href} {...(rest as Record<string, string>)}>
        {children}
      </a>
    );
  },
}));

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), patch: jest.fn(), delete: jest.fn() },
    API_KEY: "test-key",
  };
});

jest.mock("@/hooks/useActivityFeed", () => ({ useActivityFeed: jest.fn() }));

import { ApiError, api } from "@/lib/api";
import { useActivityFeed } from "@/hooks/useActivityFeed";
import { ActivityStrip } from "@/components/shell/ActivityStrip";
import { CommandPalette } from "@/components/shell/CommandPalette";
import { DashboardEditorSheet } from "@/components/dashboard/DashboardEditorSheet";
import { DocumentsOverlay } from "@/components/dashboard/DocumentsOverlay";
import { PhoneCard } from "@/components/dashboard/PhoneCard";
import { PhoneMediaSection } from "@/components/phone/PhoneMedia";
import { NewTaskDialog } from "@/components/tasks/NewTaskDialog";
import { TaskDetailContent } from "@/components/tasks/TaskDetailContent";
import { TaskDetailSheet } from "@/components/tasks/TaskDetailSheet";
import type { TaskRow } from "@/components/tasks/types";

const mockGet = api.get as jest.Mock;
const mockFeed = useActivityFeed as jest.Mock;

/* ── fixtures ─────────────────────────────────────────────────────────────────────────────── */

const TASK: TaskRow = {
  id: 1,
  title: "Draft the launch note",
  agent_type: "orchestrator",
  status: "completed",
  priority: 3,
  created_at: "2026-10-20T09:00:00Z",
};

const EVENT = {
  id: 41,
  agent_type: "scout",
  action: "deploy",
  summary: "Shipped the release",
  level: "info",
  created_at: "2026-10-24T14:36:00.000Z",
};

/** `GET /phone/media?thread_id=11` — the real envelope, one image part so the viewer can open. */
const MEDIA = {
  ok: true,
  data: {
    parts: [
      {
        part_id: 7,
        content_type: "image/png",
        size_bytes: 1024,
        when: "Today 10:00",
        thread_id: 11,
      },
    ],
    truncated: false,
  },
};

/** What the browser's own Tab order visits — the same selector the hook uses. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusablesIn(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => !element.hasAttribute("disabled") && element.tabIndex >= 0,
  );
}

beforeEach(() => {
  mockGet.mockReset();
  mockGet.mockImplementation((path: string) => {
    if (path.startsWith("/phone/media")) return Promise.resolve(MEDIA);
    // The dashboard card's config question: no store in this fixture deployment.
    if (path === "/config/phone") return Promise.resolve({ configured: false, available: false });
    return Promise.resolve([]);
  });
  mockFeed.mockReset();
  mockFeed.mockReturnValue({
    events: [EVENT],
    status: "live",
    error: null,
    attempts: 0,
    connected: true,
  });
  // The media viewer's byte fetch: refused by the store, which is a state the viewer renders.
  (global as unknown as { fetch: jest.Mock }).fetch = jest.fn().mockResolvedValue({
    ok: false,
    status: 503,
    statusText: "Service Unavailable",
    text: async () =>
      JSON.stringify({
        ok: false,
        error: {
          code: "E_PHONE_UNAVAILABLE",
          message: "the phone store is not mounted",
          how_to_fix: "set PHONE_LINK_DB_DIR",
        },
      }),
    headers: { get: () => null },
  });
});

/* ══ F-40 / A2A3-01 · the modal keyboard law, on EVERY dialog ═════════════════════════════════ */

type DialogCase = {
  name: string;
  /** Mount the surface. Components that own their open state ignore `close`. */
  mount: (close: () => void) => React.ReactNode;
  /** Any step needed to bring the dialog up (a click, a key). */
  open?: () => void | Promise<void>;
  /** The open panel. */
  panel: () => HTMLElement;
  /** True once the dialog is gone. */
  gone: () => boolean;
};

const DIALOGS: DialogCase[] = [
  {
    name: "TaskDetailSheet",
    mount: (close) => <TaskDetailSheet task={TASK} onClose={close} />,
    panel: () => screen.getByRole("dialog"),
    gone: () => screen.queryByRole("dialog") === null,
  },
  {
    name: "NewTaskDialog",
    mount: (close) => <NewTaskDialog onClose={close} />,
    panel: () => screen.getByRole("dialog"),
    gone: () => screen.queryByRole("dialog") === null,
  },
  {
    name: "DocumentsOverlay",
    mount: (close) => <DocumentsOverlay open onClose={close} />,
    panel: () => screen.getByRole("dialog", { name: "Documents" }),
    gone: () => screen.queryByRole("dialog") === null,
  },
  {
    name: "DashboardEditorSheet",
    mount: (close) => <DashboardEditorSheet open onClose={close} />,
    panel: () => screen.getByTestId("dashboard-editor"),
    gone: () => screen.queryByTestId("dashboard-editor") === null,
  },
  {
    name: "ActivityStrip (activity log)",
    mount: () => <ActivityStrip />,
    open: () => {
      fireEvent.click(screen.getByTestId("strip-log"));
    },
    panel: () => screen.getByTestId("activity-log"),
    gone: () => screen.queryByTestId("activity-log") === null,
  },
  {
    name: "PhoneMedia (media viewer)",
    mount: () => <PhoneMediaSection threadId={11} />,
    open: async () => {
      await waitFor(() => expect(screen.getByTestId("phone-media-list")).toBeInTheDocument());
      fireEvent.click(screen.getByRole("button", { name: "Open image part #7" }));
      await waitFor(() => expect(screen.getByTestId("phone-media-viewer")).toBeInTheDocument());
    },
    panel: () => screen.getByTestId("phone-media-viewer"),
    gone: () => screen.queryByTestId("phone-media-viewer") === null,
  },
  {
    name: "CommandPalette",
    mount: () => <CommandPalette />,
    open: async () => {
      fireEvent.keyDown(window, { key: "k", ctrlKey: true });
      await waitFor(() =>
        expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument(),
      );
    },
    panel: () => screen.getByRole("dialog", { name: "Command palette" }),
    gone: () => screen.queryByRole("dialog") === null,
  },
];

/** The surface's own owning component unmounts the dialog on close, exactly as its page does.
 *  The leading sentinel is the browser's OWN first tab stop: without a document-level trap, a Tab
 *  pressed with focus on `<body>` lands here — behind the scrim, outside the modal. */
function Harness({ mount }: { mount: (close: () => void) => React.ReactNode }) {
  const [open, setOpen] = useState(true);
  return (
    <>
      <button type="button">page behind the modal</button>
      {open ? mount(() => setOpen(false)) : <p data-testid="harness-closed">closed</p>}
    </>
  );
}

async function openDialog(dialogCase: DialogCase): Promise<HTMLElement> {
  render(<Harness mount={dialogCase.mount} />);
  await act(async () => {}); // the surface's own reads settle before anything is measured
  if (dialogCase.open) await dialogCase.open();
  await act(async () => {});
  return dialogCase.panel();
}

describe.each(DIALOGS)("$name — F-40/A2A3-01, the modal keyboard law", (dialogCase) => {
  it("closes on Escape after focus has LEFT the panel", async () => {
    await openDialog(dialogCase);

    // The A2A3-01 state: a mousedown on the dialog's own non-focusable text drops focus to <body>.
    (document.activeElement as HTMLElement | null)?.blur();
    expect(document.activeElement).toBe(document.body);
    expect(dialogCase.panel().contains(document.activeElement)).toBe(false);

    fireEvent.keyDown(document.body, { key: "Escape", bubbles: true });

    await waitFor(() => expect(dialogCase.gone()).toBe(true));
  });

  it("wraps Tab from its last control to the first, inside the panel", async () => {
    const user = userEvent.setup();
    const panel = await openDialog(dialogCase);
    const items = focusablesIn(panel);
    expect(items.length).toBeGreaterThan(0);

    items[items.length - 1].focus();
    await user.tab();

    expect(document.activeElement).toBe(items[0]);
    expect(panel.contains(document.activeElement)).toBe(true);
  });

  it("wraps Shift+Tab from its first control to the last, inside the panel", async () => {
    const user = userEvent.setup();
    const panel = await openDialog(dialogCase);
    const items = focusablesIn(panel);
    expect(items.length).toBeGreaterThan(0);

    items[0].focus();
    await user.tab({ shift: true });

    expect(document.activeElement).toBe(items[items.length - 1]);
    expect(panel.contains(document.activeElement)).toBe(true);
  });

  it("pulls focus back INTO the panel on Tab when focus is on <body>", async () => {
    const user = userEvent.setup();
    const panel = await openDialog(dialogCase);
    const items = focusablesIn(panel);

    (document.activeElement as HTMLElement | null)?.blur();
    expect(document.activeElement).toBe(document.body);

    await user.tab();

    // A panel-scoped handler cannot see this key at all: without the document-level trap the
    // browser's own order walks focus into the page behind the scrim.
    expect(document.activeElement).toBe(items[0]);
    expect(panel.contains(document.activeElement)).toBe(true);
  });
});

/* ══ F-37 · PhoneCard's availability gate ════════════════════════════════════════════════════ */

/** Path-routed reads; anything unlisted is a hard failure, so a stray request is visible. */
function routeReads(paths: Record<string, unknown>) {
  mockGet.mockImplementation((path: string) => {
    const value = paths[path];
    if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

describe("F-37 — PhoneCard: available:false is the unavailable state, and nothing is read from it", () => {
  it("renders the unavailable block and never asks for the 503-only summary", async () => {
    routeReads({ "/config/phone": { configured: false, available: false } });

    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    render(<PhoneCard />);

    const block = await screen.findByTestId("phone-card-unavailable");
    expect(block).toHaveTextContent("PHONE LINK UNAVAILABLE");
    expect(block).toHaveTextContent("configured: false, available: false");
    expect(mockGet.mock.calls.map(([path]) => path)).toEqual(["/config/phone"]);
    expect(screen.queryByTestId("phone-unread-total")).toBeNull();
    expect(errorSpy).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});

describe("F-37 — PhoneCard: a 503 from /config/phone is the FAILED state, named", () => {
  it("names the refusal by code, logs nothing to the console and does not guess at /phone/summary", async () => {
    routeReads({
      "/config/phone": new ApiError(
        503,
        "the config question could not be answered",
        "E_PHONE_UNAVAILABLE",
        "set PHONE_LINK_DB_DIR to the phone-link store directory and restart",
      ),
    });

    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    render(<PhoneCard />);

    const block = await screen.findByTestId("phone-card-unavailable");
    // The failed state is DISTINCT from the "no store configured" state: it says the read failed.
    expect(block).toHaveTextContent("could not be read");
    expect(block).toHaveTextContent("E_PHONE_UNAVAILABLE");
    expect(block).toHaveTextContent("set PHONE_LINK_DB_DIR");
    expect(block).not.toHaveTextContent("configured: false, available: false");

    expect(mockGet.mock.calls.map(([path]) => path)).toEqual(["/config/phone"]);
    expect(screen.queryByTestId("phone-unread-total")).toBeNull();
    // A refused read is a state the card RENDERS — it is not a console error.
    expect(errorSpy).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});

describe("F-37 — PhoneCard: the ONE 503 refusal path (config said available) still renders by name", () => {
  it("shows the summary refusal code and fix hint, and no summary data beside it", async () => {
    routeReads({
      "/config/phone": { configured: true, available: true },
      "/phone/summary": new ApiError(
        503,
        "the phone store is not mounted",
        "E_PHONE_UNAVAILABLE",
        "set PHONE_LINK_DB_DIR to the phone-link store directory and restart",
      ),
    });

    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    render(<PhoneCard />);

    const block = await screen.findByTestId("phone-card-unavailable");
    expect(block).toHaveTextContent("E_PHONE_UNAVAILABLE");
    expect(block).toHaveTextContent("set PHONE_LINK_DB_DIR");
    expect(screen.queryByTestId("phone-unread-total")).toBeNull();
    expect(screen.queryByTestId("phone-card-threads")).toBeNull();
    expect(errorSpy).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});

/* ══ F-36 · the persisted plan edge, from the DECLARED wire type ═════════════════════════════ */

describe("F-36 — TaskDetailContent renders 'Planned by' from the declared PlanEdge", () => {
  it("renders every declared key the plan carries, including the model's time estimate", async () => {
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          plan: {
            plan_day: "2026-10-24",
            depends_on: ["Research the niche", "Draft the outline"],
            side_effect: "external_post",
            estimate_usd: 12.5,
            estimate_seconds: 900,
            acceptance: "a published note and a link",
          },
        }}
      />,
    );

    const section = await screen.findByTestId("plan-edge");
    expect(within(section).getByText("Planned by")).toBeInTheDocument();
    expect(within(section).getByText("Planned on")).toBeInTheDocument();
    expect(section).toHaveTextContent("2026-10-24");
    expect(section).toHaveTextContent("Research the niche, Draft the outline");
    expect(section).toHaveTextContent("$12.50 — the model's estimate, not spend");
    // Declared in `PlanEdge` (`types.ts`) and required by the plan's own TaskSpec — a renderer
    // that drops it silently discards a fact the wire carries.
    expect(within(section).getByText("Planned time")).toBeInTheDocument();
    expect(section).toHaveTextContent("15 min — the model's estimate, not measured elapsed time");
    expect(section).toHaveTextContent("external_post");
    expect(section).toHaveTextContent("a published note and a link");
  });

  it("keeps a stated zero and a short duration honest (0 is not absence; 45s is not '0 min')", async () => {
    render(
      <TaskDetailContent
        task={{ ...TASK, plan: { depends_on: [], estimate_usd: 0, estimate_seconds: 45 } }}
      />,
    );

    const section = await screen.findByTestId("plan-edge");
    expect(section).toHaveTextContent("$0.00 — the model's estimate, not spend");
    expect(section).toHaveTextContent("45s — the model's estimate, not measured elapsed time");
  });

  it("renders NO plan section — and no invented field — for the wire the server serves TODAY", async () => {
    // `TaskResponse` (backend/app/schemas/task.py) carries no `plan` key until D1 lands the
    // computed field. The detail must show nothing rather than a placeholder provenance row.
    render(<TaskDetailContent task={TASK} />);

    await act(async () => {});
    expect(screen.queryByTestId("plan-edge")).toBeNull();
    expect(screen.queryByText("Planned by")).toBeNull();
    expect(screen.queryByText("Planned time")).toBeNull();
    expect(screen.queryByText("Planned cost")).toBeNull();
  });
});

/* ══ F-36 · the wire D1 actually computed (`TaskResponse.plan`) ═════════════════════════════════
 *
 * `backend/app/schemas/task.py` (D1) mirrors the persisted bag and does NOT rename it:
 *   depends_on            -> depends_on_IDS: the RESOLVED DAG edge, integers
 *   depends_on_titles     -> the plan's own dependency titles (strings, capped at 8)
 *   depends_on_unresolved -> titles the planner could not match to a task
 *   depends_on_cycle      -> true when the plan was not a DAG and the node was released as a root
 * A renderer that reads `depends_on` as names prints task numbers on the sheet; one that treats a
 * null `depends_on_titles` as "no dependency" states something false.
 */
describe("F-36 — TaskDetailContent reads D1's computed plan (ids vs titles)", () => {
  it("renders the dependency TITLES, never the resolved task ids", async () => {
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          plan: {
            depends_on: [42, 43],
            depends_on_titles: ["Research the niche", "Draft the outline"],
            depends_on_cycle: false,
            depends_on_unresolved: [],
            plan_day: "2026-10-24",
            side_effect: "internal_write",
            acceptance: "a published note",
            estimate_usd: 12.5,
            estimate_seconds: 900,
          },
        }}
      />,
    );

    const section = await screen.findByTestId("plan-edge");
    expect(section).toHaveTextContent("Research the niche, Draft the outline");
    expect(section).not.toHaveTextContent("42");
    expect(section).not.toHaveTextContent("43");
  });

  it("states ids AS ids when the plan recorded no titles (never 'no dependency')", async () => {
    render(
      <TaskDetailContent
        task={{ ...TASK, plan: { depends_on: [42, 43], depends_on_titles: null } }}
      />,
    );

    const section = await screen.findByTestId("plan-edge");
    expect(section).toHaveTextContent("task #42, #43 — the plan recorded task ids, not their titles");
    expect(section).not.toHaveTextContent("— the plan recorded no dependency");
  });

  it("names a dropped cycle edge instead of presenting it as a live dependency", async () => {
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          plan: {
            depends_on: [],
            depends_on_titles: ["Ship the pricing page"],
            depends_on_cycle: true,
          },
        }}
      />,
    );

    const section = await screen.findByTestId("plan-edge");
    expect(section).toHaveTextContent(
      "Ship the pricing page — dropped: the plan was not a DAG, so this task was released as a root",
    );
  });

  it("names the dependencies the planner could not resolve", async () => {
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          plan: {
            depends_on: [],
            depends_on_titles: ["Research the niche"],
            depends_on_unresolved: ["A task that was never planned"],
          },
        }}
      />,
    );

    const section = await screen.findByTestId("plan-edge");
    expect(within(section).getByText("Unresolved")).toBeInTheDocument();
    expect(section).toHaveTextContent(
      "A task that was never planned — the planner could not match these to a planned task",
    );
    expect(section).toHaveTextContent("Research the niche");
  });
});
