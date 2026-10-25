/**
 * WAVE A3 / SEAT S7 — SURFACES. The UI shows what the system actually did.
 *
 * Four confirmed defects, each with the packet's measured reproduction:
 *
 *   F-36   the plan's edge is never rendered, so a planned task shows no provenance
 *          -> `TaskDetailContent` renders `plan` ("Planned by") and invents nothing when absent.
 *   A5-02  `metadata` is undeclared and unread, so the Approve control is parsed out of prose
 *          -> the STRUCTURED `approval_required` list is the source of truth; the parser is a
 *             fallback only, and both paths pass the same strict token contract.
 *   F-40   six of seven dialogs bind Escape to the dialog node; once focus leaves, the modal
 *          cannot be closed by keyboard
 *   A2A3-01 the same dialogs have no Tab trap at all
 *          -> ONE `useDialogFocusTrap`: document-level Escape from anywhere, Tab wraps inside
 *             the panel, applied to every dialog that hand-rolled its own handler.
 *   F-37   `PhoneCard` reads the 503-only `/phone/summary` on every mount
 *          -> the card reads the 200-when-absent `GET /config/phone` (`{configured, available}`,
 *             frozen CONTRACTS §4.16) and only then asks for the summary.
 *
 * The honesty laws this file pins:
 *   - absence is absence: no plan section for a task with no plan, no estimate row for an
 *     estimate the model never stated, no Approve control for a token that would post garbage;
 *   - a measured zero ("$0.00", "0 estimate") is NOT absence and must stay visible;
 *   - a deployment with no phone store is never asked a question whose only answer is 503;
 *   - a config read that FAILS is named and does not license a guess at `/phone/summary`.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn() },
    API_KEY: "test-key",
  };
});

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

import { ApiError, api } from "@/lib/api";
import { DocumentsOverlay } from "@/components/dashboard/DocumentsOverlay";
import { PhoneCard } from "@/components/dashboard/PhoneCard";
import { NewTaskDialog } from "@/components/tasks/NewTaskDialog";
import { TaskDetailContent } from "@/components/tasks/TaskDetailContent";
import { TaskDetailSheet } from "@/components/tasks/TaskDetailSheet";
import type { TaskRow } from "@/components/tasks/types";
import { DashboardEditorSheet } from "@/components/dashboard/DashboardEditorSheet";
import { useDialogFocusTrap } from "@/components/ui/useDialogFocusTrap";

const mockGet = api.get as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
});

/* ── shared fixtures ───────────────────────────────────────────────────────────────────────── */

const TASK: TaskRow = {
  id: 1,
  title: "Draft the launch note",
  agent_type: "orchestrator",
  status: "completed",
  priority: 3,
  created_at: "2026-10-20T09:00:00Z",
};

/** What the browser's own Tab order visits — the same set the hook uses. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusablesIn(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
}

/* ══ F-40 + A2A3-01 · the modal keyboard law ═════════════════════════════════════════════════ */

/**
 * The hook's own contract, on a minimal panel. This is the RED seam for F-40: before the hook
 * exists the module does not resolve at all, and a panel-scoped Escape cannot see a key pressed
 * while focus is on `<body>`.
 */
function TrapHarness({ onClose, open = true }: { onClose: () => void; open?: boolean }) {
  const panelRef = useRef<HTMLDivElement>(null);
  useDialogFocusTrap(panelRef, onClose, open);
  return (
    <div>
      <button type="button">before the panel</button>
      <div ref={panelRef} role="dialog" aria-modal="true" aria-label="Trap harness">
        <button type="button">first control</button>
        <button type="button">last control</button>
      </div>
      <button type="button">after the panel</button>
    </div>
  );
}

describe("useDialogFocusTrap — document-level Escape + a Tab trap", () => {
  it("closes on Escape even when focus has left the panel (the A2A3-01 case)", () => {
    const onClose = jest.fn();
    render(<TrapHarness onClose={onClose} />);

    (document.activeElement as HTMLElement | null)?.blur();
    expect(document.activeElement).toBe(document.body);

    fireEvent.keyDown(document.body, { key: "Escape", bubbles: true });

    // A panel-scoped handler never sees this key: the event's target is <body>.
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("wraps Tab from the last control to the first, inside the panel", async () => {
    const user = userEvent.setup();
    render(<TrapHarness onClose={() => {}} />);

    const panel = screen.getByRole("dialog");
    const [first, last] = focusablesIn(panel);
    last.focus();
    await user.tab();

    expect(document.activeElement).toBe(first);
    expect(panel.contains(document.activeElement)).toBe(true);
  });

  it("wraps Shift+Tab from the first control to the last, inside the panel", async () => {
    const user = userEvent.setup();
    render(<TrapHarness onClose={() => {}} />);

    const panel = screen.getByRole("dialog");
    const [first, last] = focusablesIn(panel);
    first.focus();
    await user.tab({ shift: true });

    expect(document.activeElement).toBe(last);
  });

  it("binds nothing while closed — Escape is not swallowed and Tab is not trapped", async () => {
    const user = userEvent.setup();
    const onClose = jest.fn();
    render(<TrapHarness onClose={onClose} open={false} />);

    fireEvent.keyDown(document.body, { key: "Escape", bubbles: true });
    expect(onClose).not.toHaveBeenCalled();

    const panel = screen.getByRole("dialog");
    const items = focusablesIn(panel);
    items[items.length - 1].focus();
    await user.tab();
    // The browser's own order continues past the panel: proof the trap is genuinely unbound.
    expect(screen.getByRole("button", { name: "after the panel" })).toHaveFocus();
  });

  it("calls the LATEST onClose, not the one captured at mount (inline arrows re-render)", () => {
    const first = jest.fn();
    const second = jest.fn();
    const { rerender } = render(<TrapHarness onClose={first} />);
    rerender(<TrapHarness onClose={second} />);

    fireEvent.keyDown(document.body, { key: "Escape", bubbles: true });

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});

/** The sheet unmounts on close, exactly as its owning page does. */
function SheetHarness() {
  const [open, setOpen] = useState(true);
  return open ? (
    <TaskDetailSheet task={TASK} onClose={() => setOpen(false)} />
  ) : (
    <p>sheet closed</p>
  );
}

describe("TaskDetailSheet — the dialog that had only a panel-scoped Escape and no Tab trap", () => {
  beforeEach(() => {
    mockGet.mockResolvedValue([]); // the attachments read; nothing is asserted about it here
  });

  it("Escape closes the sheet after focus has left it", async () => {
    render(<SheetHarness />);
    await act(async () => {}); // let the attachments read settle so the DOM is stable
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    (document.activeElement as HTMLElement | null)?.blur();
    fireEvent.keyDown(document.body, { key: "Escape", bubbles: true });

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("Tab from its last control wraps to the first instead of walking out of the modal", async () => {
    const user = userEvent.setup();
    render(<SheetHarness />);
    await act(async () => {});

    const panel = screen.getByRole("dialog");
    const items = focusablesIn(panel);
    expect(items.length).toBeGreaterThan(1);
    items[items.length - 1].focus();
    await user.tab();

    expect(document.activeElement).toBe(items[0]);
  });
});

function EditorHarness() {
  const [open, setOpen] = useState(true);
  return (
    <>
      <button type="button">behind the editor</button>
      {open ? (
        <DashboardEditorSheet open onClose={() => setOpen(false)} />
      ) : (
        <p>editor closed</p>
      )}
    </>
  );
}

describe("DashboardEditorSheet — a modal with no Tab trap at all (A2A3-01)", () => {
  it("Tab from the last control wraps to the first, inside the panel", async () => {
    const user = userEvent.setup();
    render(<EditorHarness />);

    const panel = screen.getByTestId("dashboard-editor");
    const items = focusablesIn(panel);
    expect(items.length).toBeGreaterThan(1);
    items[items.length - 1].focus();
    await user.tab();

    expect(document.activeElement).toBe(items[0]);
    expect(panel.contains(document.activeElement)).toBe(true);
  });

  it("Escape closes it after focus has left the panel", async () => {
    render(<EditorHarness />);
    expect(screen.getByTestId("dashboard-editor")).toBeInTheDocument();

    (document.activeElement as HTMLElement | null)?.blur();
    fireEvent.keyDown(document.body, { key: "Escape", bubbles: true });

    await waitFor(() => expect(screen.queryByTestId("dashboard-editor")).toBeNull());
  });
});

function NewTaskHarness() {
  const [open, setOpen] = useState(true);
  return open ? <NewTaskDialog onClose={() => setOpen(false)} /> : <p>new task closed</p>;
}

describe("NewTaskDialog — Escape must work even after a click on the dialog's own text", () => {
  it("closes on a document-level Escape", async () => {
    render(<NewTaskHarness />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    (document.activeElement as HTMLElement | null)?.blur();
    fireEvent.keyDown(document.body, { key: "Escape", bubbles: true });

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});

function DocumentsHarness() {
  const [open, setOpen] = useState(true);
  return (
    <>
      <button type="button">behind the documents</button>
      {open ? <DocumentsOverlay open onClose={() => setOpen(false)} /> : <p>documents closed</p>}
    </>
  );
}

describe("DocumentsOverlay — Escape must work even after focus has left the panel", () => {
  it("closes on a document-level Escape", async () => {
    mockGet.mockResolvedValue({ items: [], total: 0 });
    render(<DocumentsHarness />);
    expect(await screen.findByRole("dialog", { name: "Documents" })).toBeInTheDocument();

    (document.activeElement as HTMLElement | null)?.blur();
    fireEvent.keyDown(document.body, { key: "Escape", bubbles: true });

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

/* ══ F-36 · the persisted plan edge ══════════════════════════════════════════════════════════ */

describe("F-36 — the task detail renders the plan's provenance", () => {
  beforeEach(() => {
    mockGet.mockResolvedValue([]);
  });

  it("shows 'Planned by', the dependencies, the planned cost, the side effect and the acceptance", async () => {
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          plan: {
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
    expect(section).toHaveTextContent("Research the niche, Draft the outline");
    expect(section).toHaveTextContent("$12.50 — the model's estimate, not spend");
    expect(section).toHaveTextContent("external_post");
    expect(section).toHaveTextContent("a published note and a link");
  });

  it("reads the spelling the morning cycle persists today (depends_on_titles) — never 'no dependency'", async () => {
    // `daily_cycle.py:619-628` writes `depends_on_titles` flat on the metadata bag; a server that
    // serves that spelling must not make the sheet claim the plan recorded no dependency.
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          plan: { depends_on_titles: ["Research the niche"], side_effect: "internal_write" },
        }}
      />,
    );

    const section = await screen.findByTestId("plan-edge");
    expect(section).toHaveTextContent("Research the niche");
    expect(section).not.toHaveTextContent("no dependency");
  });

  it("renders NO plan section for a task with no persisted plan (null or absent)", async () => {
    const { rerender } = render(<TaskDetailContent task={TASK} />);
    expect(screen.queryByTestId("plan-edge")).toBeNull();
    expect(screen.queryByText("Planned by")).toBeNull();

    rerender(<TaskDetailContent task={{ ...TASK, plan: null }} />);
    expect(screen.queryByTestId("plan-edge")).toBeNull();
  });

  it("keeps absence absent — no estimate row when none was stated, and a stated zero survives", async () => {
    const { rerender } = render(
      <TaskDetailContent task={{ ...TASK, plan: { depends_on: [] } }} />,
    );

    const section = await screen.findByTestId("plan-edge");
    // An empty dependency list is a real answer, stated as such — not a blank row.
    expect(section).toHaveTextContent("— the plan recorded no dependency");
    // A key the plan never carried prints nothing at all.
    expect(within(section).queryByText("Planned cost")).toBeNull();
    expect(within(section).queryByText("Side effect")).toBeNull();
    expect(within(section).queryByText("Acceptance")).toBeNull();

    rerender(<TaskDetailContent task={{ ...TASK, plan: { depends_on: [], estimate_usd: 0 } }} />);
    expect(await screen.findByTestId("plan-edge")).toHaveTextContent(
      "$0.00 — the model's estimate, not spend",
    );
  });
});

/* ══ A5-02 · structure before prose ══════════════════════════════════════════════════════════ */

describe("A5-02 — the Approve control reads the STRUCTURED fingerprints first", () => {
  beforeEach(() => {
    mockGet.mockResolvedValue([]);
  });

  it("draws one control per structured fingerprint even when the prose names none", async () => {
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          status: "awaiting_approval",
          result_summary: "parked for review",
          approval_required: [{ fingerprint: "abcd1234ef" }, { fingerprint: "zz99yy88xx" }],
        }}
      />,
    );

    await act(async () => {});
    expect(screen.getAllByRole("button", { name: "Approve" })).toHaveLength(2);
    expect(screen.getByText("abcd1234ef")).toBeInTheDocument();
    expect(screen.getByText("zz99yy88xx")).toBeInTheDocument();
    expect(screen.queryByText(/nothing to approve from here/)).toBeNull();
  });

  it("draws NO control when the structured list carries no token the mint endpoint accepts", async () => {
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          status: "awaiting_approval",
          // The prose still carries a valid token; the structured answer is the authority and
          // it is unreadable, so a control here would post garbage. Refuse, and say why.
          result_summary:
            "awaiting approval: 1 gated action(s) need a token; request one for fingerprint(s) abcd1234ef",
          approval_required: [{ fingerprint: "short" }],
        }}
      />,
    );

    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.getByText(/nothing to approve from here/)).toBeInTheDocument();
  });

  it("keeps the prose parser as the fallback for a server that serves no structured list", async () => {
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          status: "awaiting_approval",
          result_summary:
            "awaiting approval: 1 gated action(s) need a token; request one for fingerprint(s) abcd1234ef",
        }}
      />,
    );

    await act(async () => {});
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByText("abcd1234ef")).toBeInTheDocument();
  });
});

/* ══ F-37 · the phone card asks a 200-when-absent config question first ═══════════════════════ */

const SUMMARY = {
  ok: true,
  data: {
    store_status: "ok",
    unread_total: 7,
    threads: [
      {
        thread_id: 11,
        with: "Ada Lovelace",
        unread: 3,
        last_when: "Today 14:02",
        last_text: "Are we still on for dinner?",
      },
    ],
    missed_calls_today: [],
  },
};

function routeReads(paths: Record<string, unknown>) {
  mockGet.mockImplementation((path: string) => {
    const value = paths[path];
    if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

describe("F-37 — PhoneCard consumes GET /config/phone ({configured, available})", () => {
  it("does not ask for a 503-only summary when the deployment has no phone store", async () => {
    routeReads({ "/config/phone": { configured: false, available: false } });

    render(<PhoneCard />);

    const block = await screen.findByTestId("phone-card-unavailable");
    expect(block).toHaveTextContent("PHONE LINK UNAVAILABLE");
    // The honest reason names the fact the config route answered.
    expect(block).toHaveTextContent("configured: false");
    expect(mockGet).not.toHaveBeenCalledWith("/phone/summary");
    expect(screen.queryByTestId("phone-unread-total")).toBeNull();
  });

  it("distinguishes a configured-but-unavailable store from an unconfigured one", async () => {
    routeReads({ "/config/phone": { configured: true, available: false } });

    render(<PhoneCard />);

    expect(await screen.findByTestId("phone-card-unavailable")).toHaveTextContent(
      "configured: true",
    );
    expect(mockGet).not.toHaveBeenCalledWith("/phone/summary");
  });

  it("asks for the summary only when available: true, and renders what it holds", async () => {
    routeReads({
      "/config/phone": { configured: true, available: true },
      "/phone/summary": SUMMARY,
    });

    render(<PhoneCard />);

    expect(await screen.findByTestId("phone-unread-total")).toHaveTextContent("7 unread");
    expect(mockGet).toHaveBeenCalledWith("/phone/summary");
    expect(screen.queryByTestId("phone-card-unavailable")).toBeNull();
  });

  it("names a failed config read and does NOT guess at the summary", async () => {
    routeReads({
      "/config/phone": new ApiError(
        404,
        "no such route",
        "E_NOT_FOUND",
        "this build does not serve /config/phone",
      ),
    });

    render(<PhoneCard />);

    const block = await screen.findByTestId("phone-card-unavailable");
    expect(block).toHaveTextContent("could not be read");
    expect(block).toHaveTextContent("E_NOT_FOUND");
    expect(mockGet).not.toHaveBeenCalledWith("/phone/summary");
  });

  it("refuses a config answer that is not {configured, available}", async () => {
    routeReads({ "/config/phone": { available: true } });

    render(<PhoneCard />);

    expect(await screen.findByTestId("phone-card-unavailable")).toHaveTextContent(
      "{configured, available}",
    );
    expect(mockGet).not.toHaveBeenCalledWith("/phone/summary");
  });

  it("makes no claim before the answer: the first render is a skeleton, not 'no phone store'", () => {
    mockGet.mockImplementation(() => new Promise(() => {}));

    const { container } = render(<PhoneCard />);

    expect(screen.queryByTestId("phone-card-unavailable")).toBeNull();
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
  });

  it("the ONE 503 refusal path (config said available) still renders by name", async () => {
    routeReads({
      "/config/phone": { configured: true, available: true },
      "/phone/summary": new ApiError(
        503,
        "the phone store is not mounted",
        "E_PHONE_UNAVAILABLE",
        "set PHONE_LINK_DB_DIR to the phone-link store directory and restart",
      ),
    });

    render(<PhoneCard />);

    const block = await screen.findByTestId("phone-card-unavailable");
    expect(block).toHaveTextContent("E_PHONE_UNAVAILABLE");
    expect(block).toHaveTextContent("set PHONE_LINK_DB_DIR");
    expect(screen.queryByTestId("phone-unread-total")).toBeNull();
  });
});
