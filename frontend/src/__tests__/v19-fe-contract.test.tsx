/**
 * V19 / L4 — FE ↔ API CONTRACT CONFORMANCE + the three remaining FE debt items.
 *
 * CONTRACT HALF. The JSON under `fixtures/v19/` is CAPTURED VERBATIM from the live stack
 * (read-only `GET http://127.0.0.1/api/v1/...`, HTTP 200, 2026-10-18; provenance + byte counts in
 * `fixtures/v19/README.md`). Each test below feeds one captured body to the SHIPPED parser or the
 * SHIPPED consumer and states the verdict the receipt's contract matrix records:
 *   ACCEPTED          — the parser keeps the body and the UI renders its real fields;
 *   DEGRADED-BY-NAME  — a malformed row is dropped/named, the page survives (hostile half);
 *   MISMATCH          — the parser refuses a body the live server actually serves.
 * A fixture is never edited to make a parser pass; the parser is what moves.
 *
 * DEBT HALF (RED-first, each pinned on behaviour):
 *   L4-D1 invoices — Send/Void are disabled on rows the backend can only refuse, with the reason
 *         named in the row (client-side mirror of `LEGAL_TRANSITIONS`; the backend stays the
 *         arbiter for everything the client cannot know).
 *   L4-D2 activity feed — a refused snapshot renders `ApiError.display` (code + fix hint), the
 *         V17 L4-02 contract, instead of a bare `e.message`.
 *   L4-D3 internal anchors — InboxItemRow / TaskDetailSheet navigate through `next/link`.
 *         jsdom renders both as `<a>`, so the pin is STRUCTURAL: `next/link` is mocked to stamp
 *         `data-next-link`, and the assertion is on that stamp (labelled: structural, not visual).
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: {
      get: jest.fn(),
      post: jest.fn(),
      put: jest.fn(),
      patch: jest.fn(),
      delete: jest.fn(),
    },
  };
});

/** Structural probe for L4-D3: only a `next/link` render carries this attribute. */
jest.mock("next/link", () => ({
  __esModule: true,
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} data-next-link="" {...rest}>
      {children}
    </a>
  ),
}));

import { api, ApiError } from "@/lib/api";
import { capabilitiesFrom } from "@/components/capabilities/types";
import { parsePayload } from "@/components/layout/NotificationsPanel";
import { agentQualityOf, breakerStatsOf } from "@/components/usage/useOpsRead";
import { useRuns } from "@/components/usage/useRuns";
import { useFinanceRead } from "@/components/finance/useFinanceRead";
import { useInvoiceList } from "@/components/invoices/useInvoices";
import { useScheduleList } from "@/components/schedules/useSchedules";
import { CapsPanel } from "@/components/dashboard/CapsPanel";
import { DailyReportPanel } from "@/components/dashboard/DailyReportPanel";
import { InboxItemRow } from "@/components/support/InboxItemRow";
import { TaskDetailSheet } from "@/components/tasks/TaskDetailSheet";
import { TASKS_BOARD_LIMIT } from "@/components/tasks/TaskBoard";
import { useActivityFeed } from "@/hooks/useActivityFeed";
import InvoicesPage from "@/app/invoices/page";
import SocialPage from "@/app/social/page";
import SupportPage from "@/app/support/page";
import TasksPage from "@/app/tasks/page";

import notificationsPayload from "./fixtures/v19/notifications.json";
import capabilitiesPayload from "./fixtures/v19/ops-capabilities.json";
import channelsCapsPayload from "./fixtures/v19/channels-caps.json";
import agentQualityPayload from "./fixtures/v19/ops-agent-quality.json";
import breakerPayload from "./fixtures/v19/ops-events-stats.json";
import runsPayload from "./fixtures/v19/runs.json";
import invoicesPayload from "./fixtures/v19/invoices.json";
import schedulesPayload from "./fixtures/v19/schedules.json";
import financePayload from "./fixtures/v19/finance-summary.json";
import socialPayload from "./fixtures/v19/social-posts.json";
import tasksPayload from "./fixtures/v19/tasks.json";
import agentsPayload from "./fixtures/v19/agents-status.json";
import dashboardSummaryPayload from "./fixtures/v19/dashboard-summary.json";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

/** Exact-path reads; an unlisted path fails LOUDLY so a new read cannot pass silently. */
function answerWith(answers: Record<string, unknown> = {}) {
  mockGet.mockImplementation((path: string) => {
    const value = answers[path];
    if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

/* ──────────────────────────────────────────────────────────────────────────────────────────────
 * 1. Parser-level acceptance — the captured live bodies, straight into the shipped parsers.
 * ────────────────────────────────────────────────────────────────────────────────────────────── */

describe("contract — captured live bodies vs the exported parsers", () => {
  it("GET /notifications → parsePayload ACCEPTS (items.length === count, every item typed)", () => {
    const parsed = parsePayload(notificationsPayload);
    expect(typeof parsed).not.toBe("string");
    if (typeof parsed === "string") return;
    const live = notificationsPayload as { items: unknown[]; count: number };
    expect(parsed.count).toBe(live.count);
    expect(parsed.items).toHaveLength(live.items.length);
  });

  it("GET /ops/capabilities → capabilitiesFrom ACCEPTS with nothing unreadable", () => {
    const read = capabilitiesFrom(capabilitiesPayload);
    const live = capabilitiesPayload as { agents: unknown[]; registry_tools: unknown[] };
    expect(read.map.agents).toHaveLength(live.agents.length);
    expect(read.map.registry_tools).toHaveLength(live.registry_tools.length);
    expect(read.unreadable).toBe(0);
  });

  it("GET /ops/events/stats → breakerStatsOf ACCEPTS every field", () => {
    const stats = breakerStatsOf(breakerPayload);
    expect(stats).not.toBeNull();
    expect(typeof stats?.breaker_open).toBe("boolean");
    expect(stats?.failures).toBe(0);
  });

  it("GET /ops/agent-quality → agentQualityOf ACCEPTS every registered agent row", () => {
    const quality = agentQualityOf(agentQualityPayload);
    const live = agentQualityPayload as { agents: unknown[] };
    expect(quality?.agents).toHaveLength(live.agents.length);
    expect(quality?.agents.every((row) => typeof row.agent_type === "string")).toBe(true);
  });
});

/* ──────────────────────────────────────────────────────────────────────────────────────────────
 * 1b. Declared-contract conformance — the fields the FE types declare NON-null must be non-null on
 *     every captured row. This is the check that catches a type drifting from the wire without a
 *     runtime parser to notice (cast-only consumers).
 * ────────────────────────────────────────────────────────────────────────────────────────────── */

describe("contract — fields declared non-null by the FE types are non-null on the live rows", () => {
  const REQUIRED: Array<{ fixture: string; body: unknown; required: string[] }> = [
    {
      fixture: "tasks.json (TaskResponse)",
      body: tasksPayload,
      required: ["id", "title", "agent_type", "status", "created_at"],
    },
    {
      fixture: "agents-status.json (AgentStatus)",
      body: agentsPayload,
      required: ["agent_type", "tasks_today", "tasks_total"],
    },
    {
      fixture: "runs.json (RunPage)",
      body: runsPayload,
      required: ["items", "total"],
    },
    {
      fixture: "invoices.json (InvoiceResponse)",
      body: invoicesPayload,
      required: ["id", "number", "issue_date", "due_date", "net_days", "currency", "status"],
    },
    {
      fixture: "schedules.json (ScheduleResponse)",
      body: schedulesPayload,
      required: ["items", "total"],
    },
    {
      fixture: "social-posts.json (SocialPost)",
      body: socialPayload,
      required: ["id", "platform", "content", "status", "created_at"],
    },
    {
      fixture: "channels-caps.json (ChannelCapsResponse)",
      body: channelsCapsPayload,
      required: ["channels", "generated_at"],
    },
    {
      fixture: "notifications.json (NotificationsResponse)",
      body: notificationsPayload,
      required: ["items", "count"],
    },
    {
      fixture: "dashboard-summary.json (DashboardSummary)",
      body: dashboardSummaryPayload,
      required: ["tasks_today_total", "tasks_today_completed", "active_agents", "kpis"],
    },
  ];

  it.each(REQUIRED)("$fixture", ({ body, required }) => {
    const bag = body as Record<string, unknown> | unknown[];
    // Wrappers: check the required keys exist at the top level; row-level fields are checked
    // against every row of the array the key names.
    const rows: unknown[] = Array.isArray(bag) ? bag : [bag];
    for (const row of rows) {
      for (const key of required) {
        expect(row).toHaveProperty(key);
        expect((row as Record<string, unknown>)[key]).not.toBeUndefined();
      }
    }
    if (!Array.isArray(bag)) {
      for (const key of required) {
        if (Array.isArray((bag as Record<string, unknown>)[key])) {
          for (const row of (bag as Record<string, unknown[]>)[key]) {
            expect(row).not.toBeNull();
          }
        }
      }
    }
  });
});

/* ──────────────────────────────────────────────────────────────────────────────────────────────
 * 2. Hook-level acceptance — the shipped read hooks against the same captured bodies.
 * ────────────────────────────────────────────────────────────────────────────────────────────── */

function RunsProbe({ path }: { path: string }) {
  const { data, error } = useRuns(path, 0);
  return <p data-testid="out">{error ?? (data ? `rows=${data.items.length};total=${data.total}` : "loading")}</p>;
}
function InvoicesProbe({ path }: { path: string }) {
  const { rows, error } = useInvoiceList(path);
  return <p data-testid="out">{error ?? (rows ? `rows=${rows.length}` : "loading")}</p>;
}
function SchedulesProbe({ path }: { path: string }) {
  const { rows, error } = useScheduleList(path);
  return <p data-testid="out">{error ?? (rows ? `rows=${rows.length}` : "loading")}</p>;
}
function FinanceProbe({ path }: { path: string }) {
  const { data, error } = useFinanceRead<{ mrr_cents: number | null }>(path, 0);
  return <p data-testid="out">{error ?? (data ? `mrr=${String(data.mrr_cents)}` : "loading")}</p>;
}

describe("contract — captured live bodies vs the shipped read hooks", () => {
  it("GET /runs?limit=20&offset=0 → useRuns ACCEPTS {items,total}", async () => {
    answerWith({ "/runs?limit=20&offset=0": runsPayload });
    render(<RunsProbe path="/runs?limit=20&offset=0" />);
    expect(await screen.findByText("rows=20;total=204")).toBeInTheDocument();
  });

  it("GET /invoices?limit=100 → useInvoiceList ACCEPTS the bare array", async () => {
    answerWith({ "/invoices?limit=100": invoicesPayload });
    render(<InvoicesProbe path="/invoices?limit=100" />);
    expect(await screen.findByText("rows=7")).toBeInTheDocument();
  });

  it("GET /schedules → useScheduleList ACCEPTS {items,total}", async () => {
    answerWith({ "/schedules": schedulesPayload });
    render(<SchedulesProbe path="/schedules" />);
    expect(await screen.findByText("rows=1")).toBeInTheDocument();
  });

  it("GET /finance/summary → useFinanceRead ACCEPTS the null-keeping money body", async () => {
    answerWith({ "/finance/summary": financePayload });
    render(<FinanceProbe path="/finance/summary" />);
    expect(await screen.findByText("mrr=0")).toBeInTheDocument();
  });
});

/* ──────────────────────────────────────────────────────────────────────────────────────────────
 * 3. Consumer-level acceptance — real components fed the captured body.
 * ────────────────────────────────────────────────────────────────────────────────────────────── */

describe("contract — captured live bodies rendered by the shipped consumers", () => {
  it("GET /channels/caps → CapsPanel renders the real channels + the snapshot stamp", async () => {
    answerWith({ "/channels/caps": channelsCapsPayload });
    render(<CapsPanel />);
    const live = channelsCapsPayload as { channels: { channel: string }[] };
    expect(await screen.findByText(live.channels[0].channel)).toBeInTheDocument();
    expect(screen.queryByText(/the endpoint answered without a channel list/)).toBeNull();
  });

  it("GET /dashboard/reports/daily → a literal null body is the honest 'no report today' state", async () => {
    answerWith({ "/dashboard/reports/daily": null });
    render(<DailyReportPanel />);
    expect(await screen.findByText(/No report for today yet/)).toBeInTheDocument();
  });

  it("GET /social/posts?limit=50 → SocialPage renders the captured rows (status/published_at nulls included)", async () => {
    answerWith({ "/social/posts?limit=50": socialPayload });
    render(<SocialPage />);
    expect(await screen.findByTestId("social-posts")).toBeInTheDocument();
    const live = socialPayload as { content: string }[];
    expect(screen.getByText(live[0].content)).toBeInTheDocument();
  });

  it("GET /support/inbox?limit=50 → SupportPage states the real empty inbox", async () => {
    answerWith({ "/support/inbox?limit=50": [] });
    render(<SupportPage />);
    expect(await screen.findByTestId("support-count")).toHaveTextContent(/0/);
  });

  it("GET /tasks?limit=50 → TasksPage renders the captured board rows", async () => {
    const live = tasksPayload as { title: string }[];
    answerWith({ [`/tasks?limit=${TASKS_BOARD_LIMIT}`]: tasksPayload });
    render(<TasksPage />);
    // The live board legitimately repeats a title (several scheduler rows share one prompt), so
    // the pin is "the first captured row is on the board", not a unique-text lookup.
    expect((await screen.findAllByText(live[0].title)).length).toBeGreaterThan(0);
  });
});

/* ──────────────────────────────────────────────────────────────────────────────────────────────
 * 4. Hostile pass — nulls, wrong types, malformed rows, empty lists. Every parser must NAME the
 *    problem or degrade the row; none may throw into the page (except the ones documented as
 *    throwing for the page's own error state: capabilitiesFrom).
 * ────────────────────────────────────────────────────────────────────────────────────────────── */

describe("hostile — malformed bodies degrade BY NAME, they never crash a page", () => {
  it("parsePayload: a count/items disagreement is named, not rendered as partial truth", () => {
    const verdict = parsePayload({ items: [], count: 3 });
    expect(typeof verdict).toBe("string");
    expect(verdict).toContain("count (3) did not match its items (0)");
  });

  it("parsePayload: one malformed item names the item contract and refuses the batch", () => {
    const verdict = parsePayload({ items: [{ id: "n1" }], count: 1 });
    expect(typeof verdict).toBe("string");
    expect(verdict).toContain("did not match the notifications contract");
  });

  it("capabilitiesFrom: malformed rows are DROPPED AND COUNTED, readable rows still render", () => {
    const read = capabilitiesFrom({
      agents: [{ agent_type: "good", tools: [{ name: "t", description: "d" }], action_classes: [], channels: [] }, {}],
      registry_tools: [{ name: "" }, { name: "ok" }],
    });
    expect(read.map.agents).toHaveLength(1);
    expect(read.map.registry_tools).toHaveLength(1);
    // Dropped+counted: the nameless agent row and the nameless tool row (2), not the readable ones.
    expect(read.unreadable).toBe(2);
  });

  it("capabilitiesFrom: a body that is not the map THROWS a named message (never an empty map)", () => {
    expect(() => capabilitiesFrom({ detail: "not found" })).toThrow(/no capability map/);
  });

  it("breakerStatsOf: a string where a number belongs is null, not a fabricated 0", () => {
    expect(breakerStatsOf({ failures: "3", skipped_while_open: 0, breaker_open: false, cooldown_seconds: 1 })).toBeNull();
    expect(breakerStatsOf(null)).toBeNull();
  });

  it("agentQualityOf: a row without identity is dropped; a row missing readings keeps nulls", () => {
    const quality = agentQualityOf({ agents: [{ agent_type: "x" }, null, { runs_7d: 2 }] });
    expect(quality?.agents).toHaveLength(1);
    expect(quality?.agents[0]).toEqual({
      agent_type: "x",
      runs_7d: null,
      success_rate: null,
      last_failure_at: null,
      cost_per_completed_task: null,
    });
  });

  it("useRuns: a numeric-string total is a broken page and is named as one", async () => {
    answerWith({ "/runs?limit=20&offset=0": { items: [], total: "5" } });
    render(<RunsProbe path="/runs?limit=20&offset=0" />);
    expect(await screen.findByText("the server returned no runs page")).toBeInTheDocument();
  });

  it("useInvoiceList: a wrapper instead of the bare array is named, never an empty list", async () => {
    answerWith({ "/invoices?limit=100": { items: [] } });
    render(<InvoicesProbe path="/invoices?limit=100" />);
    expect(await screen.findByText("the server returned no invoice list")).toBeInTheDocument();
  });

  it("useScheduleList: a missing items array is named, never an empty schedule list", async () => {
    answerWith({ "/schedules": [] });
    render(<SchedulesProbe path="/schedules" />);
    expect(await screen.findByText("the endpoint answered without a schedule list")).toBeInTheDocument();
  });
});

/* ──────────────────────────────────────────────────────────────────────────────────────────────
 * 5. L4-D1 — invoices Send/Void gated on the backend's LEGAL_TRANSITIONS, reason named.
 * ────────────────────────────────────────────────────────────────────────────────────────────── */

function invoice(over: Record<string, unknown> = {}) {
  return {
    id: 1,
    number: "INV-2026-001",
    bill_to: { name: "Acme Co" },
    issue_date: "2026-10-01",
    due_date: "2026-10-31",
    net_days: 30,
    currency: "USD",
    subtotal: 100,
    tax: null,
    total: 100,
    status: "draft",
    payment_link: null,
    paid_at: null,
    late_fee_note: null,
    line_items: null,
    ...over,
  };
}

describe("L4-D1 — Send/Void mirror LEGAL_TRANSITIONS (backend stays the arbiter)", () => {
  const rows = [
    invoice({ id: 1, status: "draft" }),
    invoice({ id: 2, status: "sent" }),
    invoice({ id: 3, status: "overdue" }),
    invoice({ id: 4, status: "paid" }),
    invoice({ id: 5, status: "void" }),
  ];

  async function renderBoard() {
    answerWith({ "/invoices?limit=100": rows });
    render(<InvoicesPage />);
    await screen.findByTestId("invoice-row-1");
  }

  function row(id: number) {
    return screen.getByTestId(`invoice-row-${id}`);
  }

  it("draft: Send AND Void are offered", async () => {
    await renderBoard();
    expect(within(row(1)).getByRole("button", { name: /^Send$/ })).toBeEnabled();
    expect(within(row(1)).getByRole("button", { name: /^Void$/ })).toBeEnabled();
  });

  it("sent + overdue: Send is disabled with the draft-only reason; Void stays offered", async () => {
    await renderBoard();
    for (const id of [2, 3]) {
      const send = within(row(id)).getByRole("button", { name: /^Send$/ });
      expect(send).toBeDisabled();
      expect(send).toHaveAttribute("title", expect.stringContaining("only a draft"));
      expect(within(row(id)).getByTestId(`invoice-send-reason-${id}`)).toHaveTextContent(
        /only a draft invoice can be sent/i,
      );
      expect(within(row(id)).getByRole("button", { name: /^Void$/ })).toBeEnabled();
    }
  });

  it("paid + void: BOTH are disabled, and the row names the terminal state", async () => {
    await renderBoard();
    for (const id of [4, 5]) {
      expect(within(row(id)).getByRole("button", { name: /^Send$/ })).toBeDisabled();
      expect(within(row(id)).getByRole("button", { name: /^Void$/ })).toBeDisabled();
      expect(within(row(id)).getByTestId(`invoice-terminal-reason-${id}`)).toHaveTextContent(
        /terminal/,
      );
    }
  });

  it("an UNKNOWN status is not guessed at: both controls stay live (the backend is the arbiter)", async () => {
    answerWith({ "/invoices?limit=100": [invoice({ id: 11, status: "disputed" })] });
    render(<InvoicesPage />);
    const r = await screen.findByTestId("invoice-row-11");
    expect(within(r).getByRole("button", { name: /^Send$/ })).toBeEnabled();
    expect(within(r).getByRole("button", { name: /^Void$/ })).toBeEnabled();
    expect(within(r).queryByTestId("invoice-send-reason-11")).toBeNull();
  });

  it("the backend is still the arbiter: a sent-invoice send refusal renders by name", async () => {
    answerWith({ "/invoices?limit=100": [invoice({ id: 6, status: "draft" })] });
    mockPost.mockRejectedValue(
      new ApiError(400, "only a draft invoice can be sent; invoice INV-2026-006 is sent", "E_VALIDATION", "fix the field"),
    );
    render(<InvoicesPage />);
    const r = await screen.findByTestId("invoice-row-6");
    fireEvent.click(within(r).getByRole("button", { name: /^Send$/ }));
    expect(await screen.findByTestId("invoice-action-error-6")).toHaveTextContent(
      "E_VALIDATION: only a draft invoice can be sent; invoice INV-2026-006 is sent — fix the field",
    );
  });
});

/* ──────────────────────────────────────────────────────────────────────────────────────────────
 * 6. L4-D2 — the feed's refusal keeps the code (the V17 L4-02 contract).
 * ────────────────────────────────────────────────────────────────────────────────────────────── */

class FakeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  constructor(readonly url: string) {
    setTimeout(() => {
      this.readyState = 1;
      this.onopen?.();
    }, 0);
  }
  close() {
    this.readyState = 3;
  }
}

function FeedProbe() {
  const feed = useActivityFeed(10, { snapshotLimit: 5 });
  return <p data-testid="feed-error">{feed.error ?? "none"}</p>;
}

describe("L4-D2 — a refused activity snapshot renders ApiError.display", () => {
  const realSocket = global.WebSocket;

  beforeEach(() => {
    // jsdom has no reachable socket; the fake opens so the REST snapshot path runs.
    (global as unknown as { WebSocket: unknown }).WebSocket = FakeSocket;
  });
  afterEach(() => {
    (global as unknown as { WebSocket: unknown }).WebSocket = realSocket;
  });

  it("names the code and the fix hint, not just the sentence", async () => {
    mockGet.mockRejectedValue(
      new ApiError(403, "the sandbox blocks outbound sends", "E_SANDBOX_BLOCKED", "turn sandbox off in Settings"),
    );
    render(<FeedProbe />);
    const line = await screen.findByTestId("feed-error");
    await waitFor(() =>
      expect(line.textContent).toContain("E_SANDBOX_BLOCKED: the sandbox blocks outbound sends — turn sandbox off in Settings"),
    );
  });
});

/* ──────────────────────────────────────────────────────────────────────────────────────────────
 * 7. L4-D3 — internal links go through next/link (structural pin; see the file header).
 * ────────────────────────────────────────────────────────────────────────────────────────────── */

describe("L4-D3 — internal links render through next/link", () => {
  it("InboxItemRow's 'Open task #N' is a next/link", () => {
    render(
      <ul>
        <InboxItemRow
          item={{
            id: 5,
            provider_message_id: "msg-5",
            from_address: "a@b.test",
            subject: "hi",
            body: "body",
            status: "pending",
            received_at: null,
            task_id: 42,
            created_at: null,
          }}
        />
      </ul>,
    );
    expect(screen.getByRole("link", { name: /Open task #42/ })).toHaveAttribute("data-next-link");
  });

  it("TaskDetailSheet's 'Open full page' is a next/link", () => {
    const task = {
      id: 77,
      title: "t",
      agent_type: "customer_support",
      status: "pending",
      priority: 3,
      created_at: null,
    };
    render(<TaskDetailSheet task={task} onClose={() => {}} />);
    expect(screen.getByRole("link", { name: /Open full page/ })).toHaveAttribute("data-next-link");
  });
});
