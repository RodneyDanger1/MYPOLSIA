/**
 * WAVE B1 / PHASE B / SEAT C4 — name the thing, do not print its id.
 *
 * Three P2 items, each pinned by a test that fails on the defect and passes on the fix:
 *
 *   D5        `depends_on_cycle` was flattened to a boolean, so the sheet could say "a cycle was
 *             dropped" but not WHICH task it was waiting on. The producer records the member
 *             TITLES (`daily_cycle.py:703`); the sheet now names them when the wire serves the
 *             list, and keeps today's honest sentence when it serves only `true` (D1's computed
 *             field still flattens it — the backend leg is filed in REQUESTS, never faked here).
 *   D1        `approval_required` is a BOOLEAN now and the fingerprints live in
 *             `approval_fingerprints: string[]`. `Array.isArray(true)` is false, so reading the
 *             flag as the list silently fell through to the prose parser A5-02 removed — the
 *             Approve control vanished on exactly the rows the server marks as parked.
 *   A5-05/06  copy truth: the approvals surface described a capability it no longer lacked
 *             ("no readable fingerprint") while the server served one, printed a page length as
 *             the waiting total, and three dead-component headers cited `wave-v29/DECISIONS.md`,
 *             a document that exists nowhere. Every sentence below is compared against the code
 *             path that produces it; true sentences are kept.
 *
 * Verified against the tree, not assumed:
 *   - `backend/app/schemas/task.py:121-182` — `plan`, `approval_required: bool`,
 *     `approval_fingerprints: list[str]`;
 *   - `backend/celery_app/tasks/daily_cycle.py:700-705` — `depends_on_cycle = [titles…]`;
 *   - `backend/app/api/v1/tasks.py:131-158` — `GET /tasks` declares `X-Polsia-Total`;
 *   - `COMPLETENESS.md:335` — §7.2 item 7 is the retire-vs-keep record the dead headers cite.
 */
import { act, render, screen } from "@testing-library/react";
import fs from "node:fs";
import path from "node:path";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), patch: jest.fn(), delete: jest.fn() },
    API_KEY: "test-key",
  };
});

import { api } from "@/lib/api";
import { OpsApprovals, waitingLabel } from "@/components/ops/OpsApprovals";
import { fingerprintsForTask } from "@/components/tasks/approval";
import { recoveryFor } from "@/components/tasks/recovery";
import { TaskDetailContent } from "@/components/tasks/TaskDetailContent";
import type { TaskRow } from "@/components/tasks/types";

const mockGet = api.get as jest.Mock;

const ROOT = process.cwd();
const APPROVALS_PATH = "/tasks?status=awaiting_approval&limit=50";

const TASK: TaskRow = {
  id: 41,
  title: "Ship the pricing page",
  agent_type: "content",
  status: "pending",
  priority: 2,
  created_at: "2026-10-24T10:00:00Z",
};

/** The prose the parking producer writes (`agent_tasks.py:1030`). */
const PARKED_PROSE =
  "awaiting approval: 1 gated action(s) need a token; request one for fingerprint(s) fp12345678";

async function flush(times = 8) {
  await act(async () => {
    for (let i = 0; i < times; i += 1) await Promise.resolve();
  });
}

/* ══ D5 · the dropped cycle's MEMBERS ═══════════════════════════════════════════════════════════ */

describe("D5 — the task sheet names the members of the dropped cycle", () => {
  it("renders the cycle's member titles when the wire serves the list", async () => {
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          plan: {
            depends_on: [],
            // The producer's spelling (`daily_cycle.py:703`): the TITLES of the edges that could
            // never be satisfied. Abbreviated on the wire by D1's `plan` today — see the next
            // test — but this is the shape the sheet must consume the moment it is served.
            depends_on_cycle: ["Research the niche", "Draft the outline"],
            depends_on_titles: ["Research the niche", "Draft the outline"],
          },
        }}
      />,
    );

    const section = await screen.findByTestId("plan-edge");
    expect(section).toHaveTextContent(
      "Dropped cycle — Research the niche, Draft the outline: the plan was not a DAG, so these edges could never be satisfied and this task was released as a root",
    );
  });

  it("keeps today's honest sentence for the flattened boolean (no members served)", async () => {
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
    // Byte-for-byte the sentence B1Truth.test.tsx pins: a boolean wire must not regress.
    expect(section).toHaveTextContent(
      "Ship the pricing page — dropped: the plan was not a DAG, so this task was released as a root",
    );
    expect(section).not.toHaveTextContent("Dropped cycle —");
  });

  it("does not invent a cycle from an empty member list", async () => {
    render(
      <TaskDetailContent
        task={{ ...TASK, plan: { depends_on: [], depends_on_cycle: [], depends_on_titles: [] } }}
      />,
    );

    const section = await screen.findByTestId("plan-edge");
    expect(section).toHaveTextContent("— the plan recorded no dependency");
    expect(section).not.toHaveTextContent(/dropped/i);
  });

  it("names the cycle even when the plan serviced no titles (members are the fact)", async () => {
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          plan: { depends_on: [], depends_on_cycle: ["Task B"], depends_on_titles: null },
        }}
      />,
    );

    const section = await screen.findByTestId("plan-edge");
    expect(section).toHaveTextContent("Dropped cycle — Task B:");
    expect(section).not.toHaveTextContent("the plan recorded no dependency");
  });
});

/* ══ D1 · approval_required is a BOOLEAN, approval_fingerprints is the LIST ═════════════════════ */

describe("D1 — the Approve control reads approval_fingerprints, never the boolean", () => {
  it("reads the fingerprints the server serves in approval_fingerprints", () => {
    const parked: TaskRow = {
      ...TASK,
      status: "awaiting_approval",
      approval_required: true,
      approval_fingerprints: ["abcd1234ef", "zz99yy88xx"],
      result_summary: "parked for review",
    };

    expect(fingerprintsForTask(parked)).toEqual(["abcd1234ef", "zz99yy88xx"]);
  });

  it("does not resurrect a control from stale prose when the server serves an empty list", () => {
    // `approval_required: false` is a MEASURED answer (`bool([])`), not an unknown: the park was
    // cleared. A sentence left in `result_summary` must not outvote the server's own list.
    const cleared: TaskRow = {
      ...TASK,
      status: "awaiting_approval",
      approval_required: false,
      approval_fingerprints: [],
      result_summary: PARKED_PROSE,
    };

    expect(fingerprintsForTask(cleared)).toEqual([]);
    expect(recoveryFor(cleared).kind).toBe("none");
  });

  it("applies the strict token contract to the served list (a short token posts garbage)", () => {
    const unreadable: TaskRow = {
      ...TASK,
      status: "awaiting_approval",
      approval_required: true,
      approval_fingerprints: ["short"],
      result_summary: PARKED_PROSE,
    };

    expect(fingerprintsForTask(unreadable)).toEqual([]);
    expect(recoveryFor(unreadable).kind).toBe("none");
  });

  it("still reads the pre-D1 list spelling the A3 pins were written against", () => {
    const legacy: TaskRow = {
      ...TASK,
      status: "awaiting_approval",
      approval_required: [{ fingerprint: "abcd1234ef" }],
    };

    expect(fingerprintsForTask(legacy)).toEqual(["abcd1234ef"]);
  });

  it("keeps the prose parser as the fallback for a server that serves neither field", () => {
    const proseOnly: TaskRow = {
      ...TASK,
      status: "awaiting_approval",
      result_summary: PARKED_PROSE,
    };

    expect(fingerprintsForTask(proseOnly)).toEqual(["fp12345678"]);
  });

  it("draws the Approve control on the boolean wire (the measured hazard)", async () => {
    // HEAD: `Array.isArray(true)` is false -> the prose has no "fingerprint(s) …" list here -> the
    // sheet printed "nothing to approve from here" for a row the server marked as needing approval.
    render(
      <TaskDetailContent
        task={{
          ...TASK,
          status: "awaiting_approval",
          approval_required: true,
          approval_fingerprints: ["abcd1234ef"],
          result_summary: "parked for review",
        }}
      />,
    );

    await flush();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByText("abcd1234ef")).toBeInTheDocument();
    expect(screen.queryByText(/nothing to approve from here/)).toBeNull();
  });
});

/* ══ A5-05 · the approvals surface's copy vs the code path behind it ════════════════════════════ */

describe("A5-05 — Ops pending approvals reads the same wire the task sheet does", () => {
  beforeEach(() => {
    mockGet.mockReset();
  });

  function parkedRow(overrides: Record<string, unknown>): Record<string, unknown> {
    return {
      id: 3,
      title: "Review the campaign budget",
      agent_type: "ads_management",
      status: "awaiting_approval",
      result_summary: null,
      ...overrides,
    };
  }

  it("offers Approve from approval_fingerprints and drops the 'no readable fingerprint' claim", async () => {
    mockGet.mockResolvedValue([parkedRow({ approval_required: true, approval_fingerprints: ["fp12345678"] })]);

    render(<OpsApprovals tick={0} onChanged={jest.fn()} />);
    await flush();

    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.queryByText(/carries no readable fingerprint/)).toBeNull();
  });

  it("says WHICH two sources it checked when neither carries a token", async () => {
    mockGet.mockResolvedValue([
      parkedRow({ approval_required: true, approval_fingerprints: [], result_summary: "parked" }),
    ]);

    render(<OpsApprovals tick={0} onChanged={jest.fn()} />);
    await flush();

    expect(screen.getByText(/no readable fingerprint/)).toHaveTextContent(
      "its approval list (approval_fingerprints) and its result text",
    );
  });

  it("keeps the prose fallback for a server that serves no structured field", async () => {
    mockGet.mockResolvedValue([parkedRow({ result_summary: PARKED_PROSE })]);

    render(<OpsApprovals tick={0} onChanged={jest.fn()} />);
    await flush();

    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByText("fp12345678")).toBeInTheDocument();
  });
});

/* ══ A5-05 · the count is the server's declared total, never the page length ════════════════════ */

describe("A5-05 — the waiting count is the declared total, not the page", () => {
  beforeEach(() => {
    mockGet.mockReset();
  });

  it("names the page and the declared total in every shape", () => {
    // The label's whole contract, without a DOM: a capped page is never printed as the queue.
    expect(waitingLabel(50, 62)).toBe("50 of 62 waiting");
    expect(waitingLabel(2, 2)).toBe("2 waiting");
    expect(waitingLabel(50, null)).toBe("50 shown — the server declared no total");
  });

  function pageOf(count: number): Array<Record<string, unknown>> {
    return Array.from({ length: count }, (_, index) => ({
      id: index + 1,
      title: `Parked task ${index + 1}`,
      agent_type: "ads_management",
      status: "awaiting_approval",
      result_summary: null,
    }));
  }

  it("prints '<shown> of <total> waiting' when the declared total exceeds the page", async () => {
    mockGet.mockImplementation((_path: string, options?: { onHeaders?: (headers: Headers) => void }) => {
      options?.onHeaders?.(new Headers({ "X-Polsia-Total": "62" }));
      return Promise.resolve(pageOf(50));
    });

    render(<OpsApprovals tick={0} onChanged={jest.fn()} />);
    await flush();

    const label = screen.getByTestId("ops-approvals-count");
    expect(label).toHaveTextContent("50 of 62 waiting");
    // The lie HEAD printed: the page length dressed as the whole queue.
    expect(label).not.toHaveTextContent(/^50 waiting$/);
  });

  it("prints the page length WITH the reason when the server declares no total", async () => {
    mockGet.mockImplementation(() => Promise.resolve(pageOf(2)));

    render(<OpsApprovals tick={0} onChanged={jest.fn()} />);
    await flush();

    expect(screen.getByTestId("ops-approvals-count")).toHaveTextContent(
      "2 shown — the server declared no total",
    );
  });

  it("still prints the total alone when the page IS the whole queue", async () => {
    mockGet.mockImplementation((_path: string, options?: { onHeaders?: (headers: Headers) => void }) => {
      options?.onHeaders?.(new Headers({ "X-Polsia-Total": "2" }));
      return Promise.resolve(pageOf(2));
    });

    render(<OpsApprovals tick={0} onChanged={jest.fn()} />);
    await flush();

    expect(screen.getByTestId("ops-approvals-count")).toHaveTextContent("2 waiting");
  });
});

/* ══ A5-06 · a citation to a document that does not exist is a defect ══════════════════════════ */

describe("A5-06 — every doc citation in the UI resolves", () => {
  // F-12 (wave B2): AgentStatusGrid.tsx and MetricsCard.tsx were retired, so the only remaining
  // file that carries the wave-U4 why-comment and its `COMPLETENESS.md §7.2 item 7` citation is
  // TopbarPopover, whose path is outside this seat's territory (REQUESTS.md R2).
  const CITED = ["src/components/layout/TopbarPopover.tsx"];

  function read(relativePath: string): string {
    return fs.readFileSync(path.join(ROOT, relativePath), "utf8");
  }

  it("cites the in-repo retire decision, not the phantom wave-v29 file", () => {
    for (const file of CITED) {
      const source = read(file);
      expect(source).not.toMatch(/wave-v29\/DECISIONS\.md/);
      expect(source).toMatch(/COMPLETENESS\.md §7\.2 item 7/);
    }
  });

  it("the cited section exists and says what the headers claim", () => {
    const completeness = read("../COMPLETENESS.md");
    expect(completeness).toMatch(/### 7\.2 Wiring \/ observability items/);
    expect(completeness).toMatch(/Three components are imported by nothing/);
    expect(completeness).toMatch(/retire-vs-keep/i);
  });

  it("no shipped source file cites a decisions document anywhere", () => {
    const offenders: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name === "__tests__" || entry.name === "node_modules") continue;
          walk(full);
          continue;
        }
        if (!/\.(ts|tsx)$/.test(entry.name)) continue;
        if (/DECISIONS\.md/.test(fs.readFileSync(full, "utf8"))) {
          offenders.push(path.relative(ROOT, full));
        }
      }
    };
    walk(path.join(ROOT, "src"));
    expect(offenders).toEqual([]);
  });
});
