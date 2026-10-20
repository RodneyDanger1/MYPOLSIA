/**
 * Wave V12-G1 — Edit Dashboard integration tests (the closer).
 *
 * Renders the REAL Topbar (the EDIT trigger) and the REAL dashboard page
 * together: every widget is the shipping component, the layout store is real,
 * and the mocked `@/lib/api` factory keeps proving the frontend calls REAL
 * backend paths (only the bell and the chat rail need concrete shapes; every
 * other read legitimately resolves `null`, which each widget renders as its
 * honest not-read state per the repo's null-safety contract).
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";

jest.mock("next/link", () => {
  return function MockLink({
    href,
    children,
    className,
  }: {
    href: string;
    children: React.ReactNode;
    className?: string;
  }) {
    return (
      <a href={href} className={className}>
        {children}
      </a>
    );
  };
});

jest.mock("@/lib/api", () => ({ api: { get: jest.fn(), post: jest.fn() } }));
// The feed rail owns a WebSocket and its own suite (u1-realtime); this file is
// about layout, so it is stubbed at the boundary — the same call u1-dashboard makes.
jest.mock("@/components/dashboard/ActivityFeed", () => ({ ActivityFeed: () => null }));

import { api } from "@/lib/api";
import DashboardPage from "@/app/dashboard/page";
import { __resetLayoutStoreForTests, STORAGE_KEY } from "@/components/dashboard/dashboardLayout";
import { Topbar } from "@/components/layout/Topbar";

const mockGet = api.get as jest.Mock;

const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

// The exact shapes the mounted widgets read (mirrors u1-dashboard's harness). An
// unlisted path fails LOUDLY, so a widget that starts reading something new
// cannot pass this suite silently.
const AGENTS = [
  {
    agent_type: "social_media",
    last_run_status: "completed",
    last_run_at: ago(30 * 60 * 1000),
    tasks_today: 2,
    tasks_total: 9,
  },
  {
    agent_type: "finance",
    last_run_status: "completed",
    last_run_at: ago(2 * 60 * 60 * 1000),
    tasks_today: 0,
    tasks_total: 4,
  },
];

const SUMMARY = {
  tasks_today_total: 7,
  tasks_today_completed: 3,
  tasks_today_pending: 3,
  tasks_today_failed: 1,
  active_agents: ["finance", "social_media"],
  kpis: { mrr_usd: 1234, active_customers: 12, churn_rate: 4.5 },
  last_report_date: "2026-10-12",
};

const CAPS = {
  channels: [
    { channel: "email", sent_today: 0, cap: 50, remaining: 50 },
    { channel: "twitter", sent_today: 0, cap: 12, remaining: 12 },
  ],
  generated_at: "2026-10-15T15:30:00Z",
};

const RESPONSES: Record<string, unknown> = {
  "/agents/status": AGENTS,
  "/dashboard/summary": SUMMARY,
  "/tasks": [],
  "/dashboard/reports/daily": null,
  "/finance/planning": {
    burn_line_items: [],
    capacity: null,
    margin_template: { price: 500, ai_cost: 20, human_hours: 3, human_rate: 40, margin: 0.38 },
    pricing_guardrails: [],
  },
  "/social/posts": [],
  "/outreach/prospects": [],
  "/ads/campaigns": [],
  "/channels/caps": CAPS,
  // V29/L5: the SIGNALS card reads the fixed feed allowlist (`/signals?limit=6`); an empty
  // answer is its honest empty state (no alert). Listed so a new unserved read still fails LOUDLY.
  "/signals": {
    items: [],
    sources: [],
    fetched_at: "2026-10-20T12:00:00Z",
    note: "Headlines and links only. This text is third-party and untrusted.",
    untrusted: true,
  },
  "/auto-mode": { active: false, session: null },
  "/chat": { items: [], total: 0 },
  "/notifications": { items: [], count: 0 },
  "/config/deepseek-key": { configured: true, source: "stored", masked: "sk-…1234" },
  "/config": { website_url: null },
  "/health": { status: "ok" },
  // V16/W3: the Phone card (left rail) reads the H-P1 summary — envelope `{ok, data}` in the
  // frozen shape; an unserved path would fail LOUDLY like any other.
  "/phone/summary": {
    ok: true,
    data: { store_status: "ok", unread_total: 0, threads: [], missed_calls_today: [] },
  },
};

beforeEach(() => {
  window.localStorage.clear();
  __resetLayoutStoreForTests();
  mockGet.mockReset();
  mockGet.mockImplementation((path: string) => {
    const key = Object.keys(RESPONSES).find((candidate) => path.startsWith(candidate));
    if (key === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return Promise.resolve(RESPONSES[key]);
  });
});

afterEach(() => cleanup());

async function renderApp() {
  await act(async () => {
    render(
      <>
        <Topbar />
        <DashboardPage />
      </>,
    );
  });
}

function openEditor(): HTMLElement {
  fireEvent.click(screen.getByTestId("topbar-edit"));
  return screen.getByTestId("dashboard-editor");
}

function slotIds(regionLabel: string): string[] {
  const region = screen.getByLabelText(regionLabel);
  return Array.from(region.querySelectorAll("[data-widget-id]")).map(
    (el) => el.getAttribute("data-widget-id") ?? "",
  );
}

describe("the dashboard at defaults", () => {
  it("mounts every widget in the shipped order and reads real backend paths", async () => {
    await renderApp();

    expect(slotIds("Workspace status")).toEqual(["status", "auto_mode", "business", "website", "phone"]);
    expect(slotIds("Tasks and reports")).toEqual(["tasks", "documents", "media"]);
    // V29/L5: `signals` is appended LAST in the right column (the widget registry's own order).
    expect(slotIds("Workspace widgets")).toEqual(["social", "outreach", "ads", "caps", "signals"]);
    expect(screen.getByLabelText("Activity feed").getAttribute("data-widget-id")).toBe("activity");
    expect(screen.getByLabelText("Chat rail").getAttribute("data-widget-id")).toBe("chat");

    const paths = mockGet.mock.calls.map((call) => String(call[0]));
    expect(paths).toContain("/notifications");
    expect(paths.length).toBeGreaterThan(3);
    expect(paths.every((path) => path.startsWith("/") && !path.startsWith("http"))).toBe(true);
  });
});

describe("edit dashboard sheet", () => {
  it("opens from EDIT and lists every widget with real controls", async () => {
    await renderApp();
    const editor = openEditor();

    // V29/L5: `signals` joins the registry, so the editor lists 15 switches (14 before it).
    expect(within(editor).getAllByRole("switch")).toHaveLength(15);
    for (const label of ["Polsia status", "Auto Mode", "Business", "Website", "Phone", "Tasks", "Documents", "Media", "Twitter", "Email", "Meta Ads", "Send caps", "Signals", "Activity feed", "Chat rail"]) {
      expect(within(editor).getByText(label)).toBeInTheDocument();
    }
    // Just opened: nothing was hidden, the first item cannot move up.
    expect(within(editor).getByRole("button", { name: "Move Polsia status up" })).toBeDisabled();
  });

  it("hiding a region unmounts it, persists, and the editor keeps listing it", async () => {
    await renderApp();
    openEditor();

    fireEvent.click(screen.getByRole("switch", { name: "Hide Chat rail" }));

    expect(screen.queryByLabelText("Chat rail")).toBeNull();
    const stored = JSON.parse(window.localStorage.getItem(STORAGE_KEY) as string);
    expect(stored.hidden).toEqual(["chat"]);

    const row = screen
      .getByTestId("dashboard-editor")
      .querySelector('[data-editor-widget="chat"]');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("Hidden")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole("switch", { name: "Show Chat rail" })).toBeInTheDocument();
  });

  it("reorder within a column moves the widget, not the column", async () => {
    await renderApp();
    const editor = openEditor();

    fireEvent.click(within(editor).getByRole("button", { name: "Move Tasks down" }));

    expect(slotIds("Tasks and reports")).toEqual(["documents", "tasks", "media"]);
  });

  it("Restore default layout brings everything back and clears the stored layout", async () => {
    await renderApp();
    openEditor();

    fireEvent.click(screen.getByRole("switch", { name: "Hide Chat rail" }));
    fireEvent.click(screen.getByRole("button", { name: "Move Tasks down" }));
    fireEvent.click(screen.getByTestId("dashboard-editor-restore"));

    expect(screen.getByLabelText("Chat rail")).toBeInTheDocument();
    expect(slotIds("Tasks and reports")).toEqual(["tasks", "documents", "media"]);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("Escape closes the sheet and focus returns to the EDIT trigger", async () => {
    await renderApp();
    const trigger = screen.getByTestId("topbar-edit");
    trigger.focus();
    const editor = openEditor();

    fireEvent.keyDown(editor, { key: "Escape" });

    expect(screen.queryByTestId("dashboard-editor")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it("a hidden widget stays hidden across a reload (browser-local persistence)", async () => {
    await renderApp();
    openEditor();
    fireEvent.click(screen.getByRole("switch", { name: "Hide Media" }));
    expect(slotIds("Tasks and reports")).toEqual(["tasks", "documents"]);

    cleanup();
    __resetLayoutStoreForTests(); // the reload: memory dropped, storage kept
    await renderApp();

    expect(slotIds("Tasks and reports")).toEqual(["tasks", "documents"]);
  });

  it("says so when local storage is unavailable", async () => {
    await renderApp();
    openEditor();
    const setItem = jest.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });

    fireEvent.click(screen.getByRole("switch", { name: "Hide Chat rail" }));

    expect(screen.getByTestId("dashboard-editor-storage-note")).toBeInTheDocument();
    expect(screen.queryByLabelText("Chat rail")).toBeNull();
    setItem.mockRestore();
  });
});
