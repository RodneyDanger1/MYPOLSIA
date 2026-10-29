import { act, renderHook, waitFor } from "@testing-library/react";

/**
 * F-38 — a late chat-history read must never wipe the exchange the user just sent.
 *
 * Measured defect at HEAD: `load()` ended with `setItems(data.items)`. The mount read is issued
 * once; a send that lands while that read is still on the wire appends two optimistic rows, and
 * the read then REPLACES the list with a body that predates the send. The transcript silently
 * loses the exchange the user just watched appear.
 *
 * Pins: (1) a stale read leaves the local exchange on screen and `total` stays the server's
 * declared count (never a +2 guess); (2) a later read that DOES carry the exchange replaces the
 * local copies with the wire rows (no duplicates, `unconfirmed` back to 0); (3) two identical
 * sends stay two rows.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), patch: jest.fn() } };
});

import { api } from "@/lib/api";
import { mergeHistory, useChat, type ChatItem } from "@/components/chat/useChat";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const REPLY = {
  reply: { text: "Three tasks completed today." },
  intent: { kind: "answer" },
  task_id: null,
  requires_confirm: false,
  confirm_token: null,
  refusal: null,
};

const wire = (role: string, content: string, created_at: string) => ({
  role,
  content,
  created_at,
  task_id: null,
});

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

describe("F-38 — the late history read merges, never replaces", () => {
  it("keeps the exchange just sent when the mount read lands afterwards", async () => {
    const stale = deferred<{ items: never[]; total: number }>();
    mockGet.mockReturnValueOnce(stale.promise);
    mockPost.mockResolvedValueOnce(REPLY);

    const { result } = renderHook(() => useChat());
    expect(result.current.status).toBe("loading");

    // The user sends while the history read is still in flight.
    await act(async () => {
      await result.current.send("status?");
    });
    expect(result.current.items.map((item) => item.content)).toEqual([
      "status?",
      "Three tasks completed today.",
    ]);

    // The read now lands with a body that predates the send.
    await act(async () => {
      stale.resolve({ items: [], total: 0 });
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    expect(result.current.items.map((item) => item.content)).toEqual([
      "status?",
      "Three tasks completed today.",
    ]);
    // Both are honestly labelled as not-yet-read, and the count is the SERVER's — not total + 2.
    expect(result.current.unconfirmed).toBe(2);
    expect(result.current.total).toBe(0);
  });

  it("retires the local copies once a read carries them (no duplicate rows)", async () => {
    const stale = deferred<{ items: never[]; total: number }>();
    mockGet.mockReturnValueOnce(stale.promise);
    mockPost.mockResolvedValueOnce(REPLY);

    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.send("status?");
    });
    await act(async () => {
      stale.resolve({ items: [], total: 0 });
    });

    mockGet.mockResolvedValueOnce({
      items: [
        wire("owner", "status?", "2026-10-25T10:00:00Z"),
        wire("meridian", "Three tasks completed today.", "2026-10-25T10:00:01Z"),
      ],
      total: 2,
    });
    await act(async () => {
      await result.current.load();
    });

    expect(result.current.items).toHaveLength(2);
    expect(result.current.unconfirmed).toBe(0);
    expect(result.current.total).toBe(2);
    // The rendered rows are the SERVER's now (wire fingerprints, not local sequence keys).
    expect(result.current.items.every((item) => item.key.startsWith("wire:"))).toBe(true);
  });

  it("merges identical sends as a multiset: two sends need two wire rows", () => {
    const local = (key: string): ChatItem => ({
      role: "owner",
      content: "status?",
      created_at: "2026-10-25T10:00:00Z",
      task_id: null,
      key,
      local: true,
    });
    const prev = [local("local:1"), local("local:2")];

    // A read carrying ONE of them retires one local copy, not both.
    const one = mergeHistory([wire("owner", "status?", "2026-10-25T09:59:00Z")], prev);
    expect(one.filter((item) => item.local)).toHaveLength(1);

    const two = mergeHistory(
      [
        wire("owner", "status?", "2026-10-25T09:59:00Z"),
        wire("owner", "status?", "2026-10-25T09:59:01Z"),
      ],
      prev,
    );
    expect(two.filter((item) => item.local)).toHaveLength(0);
    expect(two).toHaveLength(2);
  });
});
