import fs from "node:fs";
import path from "node:path";

import { render, screen } from "@testing-library/react";

/**
 * F-42 — the chat rail's copy must promise only reads the router can answer.
 *
 * Measured defect at HEAD: the empty state said "Ask about your tasks or spend", while the
 * classifier's answer stems carry neither a `spend`/`spent` stem in `_FINANCE_RE` nor a plural
 * `tasks` stem in `_DASHBOARD_RE` — both questions fell to the generic capabilities answer. The
 * copy now names the six things the router actually answers, and this suite checks that claim
 * against the classifier's own source (the same way B1C checks doc citations), so the copy cannot
 * drift back into a promise nothing can keep.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), patch: jest.fn() } };
});

import { api } from "@/lib/api";
import { ChatRail } from "@/components/chat/ChatRail";

const mockGet = api.get as jest.Mock;

const ROOT = process.cwd();

/**
 * The classifier's ANSWER stems, compiled from `chat_service.py` exactly as shipped: every
 * `r"..."` literal between `_LOOKUP_RE` and the agent-keyword table. `_LOOKUP_RE` first, because
 * `task 12` is the one lookup that must keep winning over the newer `tasks` promises.
 */
function answerStems(): RegExp {
  const source = fs.readFileSync(
    path.join(ROOT, "../backend/app/services/chat_service.py"),
    "utf8",
  );
  const start = source.indexOf("_LOOKUP_RE");
  const end = source.indexOf("_AGENT_KEYWORDS");
  expect(start).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  const block = source.slice(start, end);
  const literals = [...block.matchAll(/\br"((?:[^"\\]|\\.)*)"/g)].map((match) => match[1]);
  expect(literals.length).toBeGreaterThanOrEqual(5);
  return new RegExp(literals.join("|"), "i");
}

beforeEach(() => {
  mockGet.mockReset();
  mockGet.mockResolvedValue({ items: [], total: 0 });
});

describe("F-42 — the rail's empty-state copy is a true promise", () => {
  it("names exactly the reads the classifier answers", async () => {
    render(<ChatRail />);
    const copy = (await screen.findByText(/No messages yet/)).textContent ?? "";
    const stems = answerStems();

    // Every word the copy promises must be a word the router routes.
    for (const promise of ["status", "runs", "finance", "report", "task 12", "create a task"]) {
      expect(copy.toLowerCase()).toContain(promise);
      expect({ promise, answers: stems.test(promise) }).toEqual({ promise, answers: true });
    }

    // The two promises nothing could answer are gone.
    expect(copy).not.toMatch(/about your tasks/i);
    expect(copy).not.toMatch(/\bspend\b/i);
  });

  it("if the copy ever promises 'spend', the router must answer it first", async () => {
    render(<ChatRail />);
    const copy = (await screen.findByText(/No messages yet/)).textContent ?? "";
    if (/\bspend/i.test(copy)) {
      // Today this branch is not taken; the day someone restores the friendlier promise, the
      // classifier must have grown the stem (REQUESTS.md R1 owns that edit).
      expect(answerStems().test("how much did we spend on ads")).toBe(true);
    }
  });
});
