/**
 * WAVE 5 / F-36 — an INTERRUPTED run must say so where the owner reads it.
 *
 * THE DEFECT (measured live on the wave-4 build): `/runs/314` — a wave-2 run the stale-run sweep
 * closed — rendered `CANCELLED`, `FINISHED 7:51 PM` (the sweep time, not the run's own end),
 * `DURATION —`, `COST —`, `COST SOURCE —` and NO word about interruption or an unmeasured cost.
 * The wire now carries the F-26 markers (`cost_scope: "unmeasured"`, `interrupted_reason:
 * "E_RUN_STALE"` — SV-W4-04), but no screen consumed them: "cancelled by the sweeper, cost unknown"
 * and "not measured yet" were indistinguishable.
 *
 * WHAT THIS PINS:
 *   * an interrupted run renders a named marker: `interrupted · <cost scope> · <reason code>`;
 *   * `unmeasured` states the cost was never measured — the dash is an unknown, never a zero;
 *   * `in_flight_partial` states the cost is a LOWER BOUND measured up to the interruption, not the
 *     run's total (run 320's live shape);
 *   * the FINISHED time is named as the ledger's closing time, not a completion;
 *   * an unknown scope word / unknown reason code is rendered VERBATIM with "not one this build
 *     knows" — never folded into a happier reading;
 *   * a marker that is missing, wrong-typed or on a pre-SV-W4-04 server is UNMEASURED (the two
 *     field rows read the em dash); a settled run shows no interruption banner at all;
 *   * the markers are consumed from the wire (`/runs/{id}`) with no coercion: `42` is not a scope
 *     and an array is not a reason.
 */
import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), patch: jest.fn() } };
});

import { api } from "@/lib/api";
import RunDetailPage from "@/app/runs/[id]/page";

const mockGet = api.get as jest.Mock;

const EMPTY_LISTING = {
  run_id: "314",
  items: [],
  total: 0,
  total_bytes: 0,
  truncated: false,
  skipped_links: 0,
};

/** The live wave-4 shape of run 314 (the stranded run the stale-run sweep closed). */
const RUN_314 = {
  id: 314,
  task_id: 315,
  task_title: "Write a brand one-pager and a label-data checklist",
  agent_type: "code_generation",
  run_type: "task",
  status: "cancelled",
  attempt: 1,
  started_at: "2026-10-28T21:11:47.897247Z",
  ended_at: "2026-10-29T00:51:57.374435Z",
  duration_secs: null,
  tokens_input: null,
  tokens_output: null,
  tokens_used: null,
  cost_usd: null,
  cost_source: null,
  pricing_version: null,
  num_turns: null,
  is_error: null,
  tools_used: null,
  cost_scope: "unmeasured",
  interrupted_reason: "E_RUN_STALE",
};

/** Run 320's live shape: the provider loop measured a cost before the kill → a lower bound. */
const RUN_320 = {
  ...RUN_314,
  id: 320,
  task_id: 315,
  status: "cancelled",
  ended_at: "2026-10-29T00:52:02.000000Z",
  cost_usd: 0.0023,
  cost_source: "provider_usage",
  cost_scope: "in_flight_partial",
  interrupted_reason: "E_RUN_STALE",
};

function answerWith(run: Record<string, unknown>): void {
  mockGet.mockImplementation((path: string) => {
    if (path === `/runs/${run.id}`) return Promise.resolve(run);
    if (path === `/artifacts/${run.id}`) return Promise.resolve(EMPTY_LISTING);
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

beforeEach(() => {
  mockGet.mockReset();
});

it("an unmeasured interrupted run names the interruption and keeps the cost an unknown", async () => {
  answerWith(RUN_314);

  render(<RunDetailPage params={{ id: "314" }} />);

  const headline = await screen.findByTestId("run-interruption-headline");
  expect(headline).toHaveTextContent("interrupted · unmeasured cost · E_RUN_STALE");

  const detail = screen.getByTestId("run-interruption-detail");
  // The reason is explained, not just coded.
  expect(detail).toHaveTextContent(/stale-run sweep/i);
  expect(detail).toHaveTextContent(/worker stopped reporting/i);
  // The cost is UNMEASURED — an unknown, never a zero.
  expect(detail).toHaveTextContent(/never measured/i);
  expect(detail).toHaveTextContent(/not a zero/i);
  // FINISHED is the closing time, and the marker says so.
  expect(detail).toHaveTextContent(/closing time/i);

  // The ledger's own reading is untouched: no fabricated $0.00.
  expect(screen.getByTestId("run-cost")).toHaveTextContent("—");
  expect(screen.getByTestId("run-cost")).not.toHaveTextContent("$0.00");
  // The two markers are readable as their own fields too.
  expect(screen.getByTestId("run-cost-scope")).toHaveTextContent("unmeasured");
  expect(screen.getByTestId("run-interrupted-reason")).toHaveTextContent("E_RUN_STALE");
});

it("an in-flight partial cost is a LOWER BOUND, never the run's total", async () => {
  answerWith(RUN_320);

  render(<RunDetailPage params={{ id: "320" }} />);

  const headline = await screen.findByTestId("run-interruption-headline");
  expect(headline).toHaveTextContent("interrupted · partial cost (lower bound) · E_RUN_STALE");

  const detail = screen.getByTestId("run-interruption-detail");
  expect(detail).toHaveTextContent(/lower bound/i);
  expect(detail).toHaveTextContent(/not the run's total/i);
  // The measured cents are still shown honestly (they are real, just incomplete).
  expect(screen.getByTestId("run-cost")).toHaveTextContent("$0.0023");
});

it("an unknown scope or reason is rendered verbatim and flagged as unknown", async () => {
  answerWith({
    ...RUN_314,
    cost_scope: "provider_partial_v2",
    interrupted_reason: "E_SOMETHING_NEW",
  });

  render(<RunDetailPage params={{ id: "314" }} />);

  const headline = await screen.findByTestId("run-interruption-headline");
  expect(headline).toHaveTextContent("provider_partial_v2");
  expect(headline).toHaveTextContent("E_SOMETHING_NEW");

  const detail = screen.getByTestId("run-interruption-detail");
  expect(detail).toHaveTextContent(/provider_partial_v2/);
  expect(detail).toHaveTextContent(/not one this build knows/i);
  expect(detail).toHaveTextContent(/E_SOMETHING_NEW/);
});

it("an interruption reason with no recorded cost scope says the scope is missing", async () => {
  answerWith({ ...RUN_314, cost_scope: null, interrupted_reason: "E_RUN_STALE" });

  render(<RunDetailPage params={{ id: "314" }} />);

  const headline = await screen.findByTestId("run-interruption-headline");
  expect(headline).toHaveTextContent("interrupted · cost scope not recorded · E_RUN_STALE");
  expect(screen.getByTestId("run-interruption-detail")).toHaveTextContent(
    /carries no cost scope/i,
  );
});

it("a settled run shows no interruption banner and both markers as the em dash", async () => {
  answerWith({
    ...RUN_314,
    status: "completed",
    cost_usd: 0.0123,
    cost_source: "token_estimate",
    pricing_version: "2026-10-01",
    cost_scope: null,
    interrupted_reason: null,
  });

  render(<RunDetailPage params={{ id: "314" }} />);

  await screen.findByTestId("run-detail-card");
  expect(screen.queryByTestId("run-interruption")).toBeNull();
  expect(screen.queryByTestId("run-interruption-headline")).toBeNull();
  expect(screen.getByTestId("run-cost-scope")).toHaveTextContent("—");
  expect(screen.getByTestId("run-interrupted-reason")).toHaveTextContent("—");
  expect(screen.getByTestId("run-cost")).toHaveTextContent("$0.0123");
});

it("never coerces a wrong-typed marker into an interruption claim", async () => {
  answerWith({ ...RUN_314, cost_scope: 42, interrupted_reason: ["E_RUN_STALE"] });

  render(<RunDetailPage params={{ id: "314" }} />);

  await screen.findByTestId("run-detail-card");
  expect(screen.queryByTestId("run-interruption")).toBeNull();
  expect(screen.getByTestId("run-cost-scope")).toHaveTextContent("—");
  expect(screen.getByTestId("run-interrupted-reason")).toHaveTextContent("—");
});

it("a pre-SV-W4-04 server (markers absent) is unmeasured, not an interruption", async () => {
  const { cost_scope: _scope, interrupted_reason: _reason, ...older } = RUN_314;
  answerWith(older);

  render(<RunDetailPage params={{ id: "314" }} />);

  await screen.findByTestId("run-detail-card");
  expect(screen.queryByTestId("run-interruption")).toBeNull();
  expect(screen.getByTestId("run-cost-scope")).toHaveTextContent("—");
  expect(screen.getByTestId("run-interrupted-reason")).toHaveTextContent("—");
});
