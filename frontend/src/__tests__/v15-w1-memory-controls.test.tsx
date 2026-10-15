import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * WAVE V15 / W1 — item 4-05(c) / S3-03 (#6 LOCKED): the owner add/forget controls on /memory.
 *
 * The routes SHIP (`backend/app/api/v1/memory.py`): GET /memory (search/list), POST /memory
 * (`MemoryCreate`: category/title/content required strings; source/tags optional) answering 201
 * with the stored `MemoryEntryResponse`, DELETE /memory/{id:int} answering the REMOVED entry's
 * representation. The page was search-only, so this pin fixes the new DOM/network contract:
 *   - add: validated client-side by name (no request for blanks), exact POST body, success notice
 *     naming the stored entry, and the refreshed search showing it ("add -> search shows it");
 *   - forget: two-step per row (first click MUST NOT fire the DELETE), DELETE /memory/{id}, and
 *     the row GONE after the 200; a named refusal keeps the row and names the error;
 *   - a row with no addressable integer id offers no forget control and says why — the page never
 *     prints the word "null" for it (the shipped hostile-row pin).
 */

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import MemoryPage, { type Memory } from "@/app/memory/page";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockDelete = api.delete as jest.Mock;

afterEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockDelete.mockReset();
});

const storedRow: Memory = {
  id: 3,
  category: "ops",
  title: "invoice cadence",
  content: "invoices go out monthly",
  created_at: null,
  fallback: null,
};

async function searchFor(q: string) {
  // userEvent's async APIs keep the search's post-await state updates inside act (the u3 pattern) —
  // a raw fireEvent leaves them outside and every microtask-arriving setState logs a warning.
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText("Search company memory"), { target: { value: q } });
  await user.click(screen.getByRole("button", { name: "Search" }));
}

async function openAddForm() {
  fireEvent.click(screen.getByRole("button", { name: "Add memory" }));
  await screen.findByTestId("memory-add-form");
}

function fillRequired(title: string, content: string, category = "insights") {
  fireEvent.change(screen.getByLabelText("Category"), { target: { value: category } });
  fireEvent.change(screen.getByLabelText("Title"), { target: { value: title } });
  fireEvent.change(screen.getByLabelText("Content"), { target: { value: content } });
}

describe("4-05(c) — memory add/forget controls (real routes, honest states)", () => {
  it("add: exact POST body, success notice, and the NEXT search shows the stored entry", async () => {
    mockGet.mockResolvedValueOnce([]).mockResolvedValue([storedRow]);
    mockPost.mockResolvedValue({
      id: 3,
      category: "ops",
      title: "invoice cadence",
      content: "invoices go out monthly",
      source: null,
      tags: null,
    });

    render(<MemoryPage />);

    // A search first: empty store.
    await searchFor("invoice");
    await screen.findByText(/No entries matched/);

    await openAddForm();
    fillRequired("invoice cadence", "invoices go out monthly", "ops");
    fireEvent.change(screen.getByLabelText("Source (optional)"), {
      target: { value: "owner note" },
    });
    fireEvent.change(screen.getByLabelText("Tags (optional, comma-separated)"), {
      target: { value: "finance, recurring" },
    });
    // The add resolves into the auto-refresh of the displayed search — keep the whole chain inside
    // act so the state updates it drives are the user-visible ones, not an act warning.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Store memory" }));
    });

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][0]).toBe("/memory");
    expect(mockPost.mock.calls[0][1]).toEqual({
      category: "ops",
      title: "invoice cadence",
      content: "invoices go out monthly",
      source: "owner note",
      tags: ["finance", "recurring"],
    });

    // The stored entry is NAMED in the notice (id included — the route's real response).
    expect(await screen.findByTestId("memory-add-notice")).toHaveTextContent("invoice cadence");
    // The displayed search re-ran against the updated store and now shows it.
    await screen.findByText("invoice cadence");
    expect(screen.queryByText(/No entries matched/)).toBeNull();
    expect(mockGet.mock.calls.map(([p]) => p)).toEqual([
      "/memory?q=invoice&limit=20",
      "/memory?q=invoice&limit=20",
    ]);
  });

  it("add: a blank required field is refused BY NAME with no request", async () => {
    render(<MemoryPage />);
    await openAddForm();

    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "half filled" } });
    fireEvent.change(screen.getByLabelText("Content"), { target: { value: "body" } });
    fireEvent.click(screen.getByRole("button", { name: "Store memory" }));

    expect(mockPost).not.toHaveBeenCalled();
    expect(screen.getByText(/Missing required field: category/)).toBeInTheDocument();
  });

  it("add: a refused store renders the server's named refusal and keeps the draft", async () => {
    mockPost.mockRejectedValue(
      new ApiError(503, "vector store unavailable", "E_STORAGE", "retry once the index is back"),
    );

    render(<MemoryPage />);
    await openAddForm();
    fillRequired("kept draft", "must survive the refusal");
    fireEvent.click(screen.getByRole("button", { name: "Store memory" }));

    const alert = await screen.findByTestId("memory-add-error");
    expect(alert).toHaveTextContent("E_STORAGE");
    expect(alert).toHaveTextContent("retry once the index is back");
    // The draft is NOT silently cleared by a failure.
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("kept draft");
    expect((screen.getByLabelText("Content") as HTMLInputElement).value).toBe(
      "must survive the refusal",
    );
  });

  it("forget: two-step confirm, route called with the id, row GONE after the 200", async () => {
    mockGet.mockResolvedValue([storedRow]);
    mockDelete.mockResolvedValue({
      id: 3,
      category: "ops",
      title: "invoice cadence",
      content: "invoices go out monthly",
      source: null,
      tags: null,
    });

    render(<MemoryPage />);
    await searchFor("invoice");
    await screen.findByText("invoice cadence");

    // First click only arms the confirm — the route must NOT be called yet.
    fireEvent.click(screen.getByRole("button", { name: "Forget" }));
    expect(mockDelete).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Confirm forget" }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledTimes(1));
    expect(mockDelete.mock.calls[0][0]).toBe("/memory/3");

    expect(await screen.findByTestId("memory-forget-notice")).toHaveTextContent("invoice cadence");
    expect(screen.queryByText("invoice cadence")).toBeNull();
    expect(mockDelete.mock.calls[0][1]).toBeUndefined();
  });

  it("forget: a named refusal keeps the row and shows the error", async () => {
    mockGet.mockResolvedValue([storedRow]);
    mockDelete.mockRejectedValue(
      new ApiError(404, "memory 3 does not exist", "E_NOT_FOUND", "re-run the search"),
    );

    render(<MemoryPage />);
    await searchFor("invoice");
    await screen.findByText("invoice cadence");

    fireEvent.click(screen.getByRole("button", { name: "Forget" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm forget" }));

    const alert = await screen.findByTestId("memory-forget-error");
    expect(alert).toHaveTextContent("E_NOT_FOUND");
    expect(alert).toHaveTextContent("re-run the search");
    // The row is still there — a failed delete must not pretend otherwise.
    expect(screen.getByText("invoice cadence")).toBeInTheDocument();
    // And the control falls back to the un-armed state (no stale confirm).
    expect(screen.getByRole("button", { name: "Forget" })).toBeInTheDocument();
  });

  it("a row with no addressable integer id offers no forget control — and never the word null", async () => {
    mockGet.mockResolvedValue([
      { id: null, category: null, title: null, content: null, created_at: null, fallback: null },
      { id: "chroma-abc", category: "x", title: "semantic hit", content: "c", created_at: null, fallback: null },
    ]);

    render(<MemoryPage />);
    await searchFor("alpha");
    await screen.findByText("untitled");

    expect(screen.queryByRole("button", { name: "Forget" })).toBeNull();
    expect(screen.getAllByTestId("memory-forget-unavailable").length).toBe(2);
    expect(screen.queryByText(/null/)).toBeNull();
  });
});
