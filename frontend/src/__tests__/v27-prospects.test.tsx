/**
 * V27 / lane L5 — the `/prospects` page (owner-managed prospect CRM).
 *
 * Written RED-FIRST on the V26 settle tree, where `src/app/prospects/page.tsx` does not exist
 * (`raw/red-frontend.txt` is the jest import failure) and `NAV_ITEMS` has no `/prospects` row.
 *
 * House pattern (u11-schedules / u9-planner): mock `@/lib/api` over `jest.requireActual` (so
 * `ApiError` stays REAL) and pin the page on BEHAVIOUR — the real endpoint path and payload,
 * the server's answer as the only truth, the three read states, and the named refusal inline.
 *
 * Wire truth (`backend/app/api/v1/prospects.py`, `app/schemas/prospect.py`):
 *   GET    /api/v1/prospects?search=&status=&limit=  -> {items: ProspectResponse[], total: n}
 *   POST   /api/v1/prospects                        -> 201 ProspectResponse
 *   PATCH  /api/v1/prospects/{id}                   -> 200 ProspectResponse (provided keys only)
 *   DELETE /api/v1/prospects/{id}                   -> 204
 * A duplicate/blank address is `E_VALIDATION` (422) with `context.field = "email"`; an unknown
 * id is `E_NOT_FOUND` (404). The page renders `ApiError.display`.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

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

import { NAV_ITEMS } from "@/components/layout/nav-items";
import ProspectsPage from "@/app/prospects/page";
import { ApiError, api } from "@/lib/api";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockPatch = api.patch as jest.Mock;
const mockDelete = api.delete as jest.Mock;

const LIST_PATH = "/prospects?limit=100";

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPatch.mockReset();
  mockDelete.mockReset();
});

/** Exact-path reads; an unlisted path fails LOUDLY so a new read cannot pass silently. */
function answerWith(answers: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = {
    [LIST_PATH]: { items: [], total: 0 },
    ...answers,
  };
  mockGet.mockImplementation((path: string) => {
    const value = paths[path];
    if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

/** One prospect in the frozen `ProspectResponse` shape (`app/schemas/social.py`). */
function prospect(over: Record<string, unknown> = {}) {
  return {
    id: 7,
    email: "ada@example.com",
    first_name: "Ada",
    last_name: "Lovelace",
    company: "Analytical Engines",
    title: "Founder",
    source: "manual",
    status: "new",
    created_at: "2026-10-01T09:00:00Z",
    ...over,
  };
}

describe("ProspectsPage — the read", () => {
  it("reads the list and states the honest empty list (with the add form still offered)", async () => {
    answerWith();
    render(<ProspectsPage />);

    const empty = await screen.findByTestId("prospects-empty");
    expect(empty).toHaveTextContent(/No prospects yet/);
    expect(mockGet).toHaveBeenCalledWith(LIST_PATH);
    expect(screen.queryByTestId("prospects-read-error")).toBeNull();
    // an empty table is not a reason to hide the only way to fill it
    expect(screen.getByTestId("prospect-create")).toBeInTheDocument();
  });

  it("renders every server field and nulls as the em dash (never an invented value)", async () => {
    answerWith({
      [LIST_PATH]: {
        items: [
          prospect(),
          prospect({
            id: 8,
            email: "nobody@example.com",
            first_name: null,
            last_name: null,
            company: null,
            title: null,
            source: null,
            status: null,
          }),
        ],
        total: 2,
      },
    });
    render(<ProspectsPage />);

    const first = await screen.findByTestId("prospect-row-7");
    expect(within(first).getByTestId("prospect-email")).toHaveTextContent("ada@example.com");
    expect(within(first).getByTestId("prospect-name")).toHaveTextContent("Ada Lovelace");
    expect(within(first).getByTestId("prospect-company")).toHaveTextContent("Analytical Engines");
    expect(within(first).getByTestId("prospect-status")).toHaveTextContent("new");

    const second = screen.getByTestId("prospect-row-8");
    expect(within(second).getByTestId("prospect-name")).toHaveTextContent("—");
    expect(within(second).getByTestId("prospect-company")).toHaveTextContent("—");
    // null status keeps the app-wide em dash, not a fabricated "new"
    expect(within(second).getByTestId("prospect-status")).toHaveTextContent("—");
  });

  it("shows a loading state before the read settles and names a failed read", async () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    const { unmount } = render(<ProspectsPage />);
    expect(screen.getByTestId("prospects-loading")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText(/No prospects yet/)).toBeNull();
    unmount();

    answerWith({
      [LIST_PATH]: new ApiError(503, "database unreachable", "E_DB", "check DATABASE_URL"),
    });
    render(<ProspectsPage />);
    const alert = await screen.findByTestId("prospects-read-error");
    expect(alert).toHaveTextContent("Could not load the prospects: E_DB: database unreachable");
    expect(screen.queryByText(/No prospects yet/)).toBeNull();
  });

  it("sends the committed search and status filters on the read path", async () => {
    const user = userEvent.setup();
    answerWith({
      [`${LIST_PATH}&search=acme`]: { items: [], total: 0 },
      [`${LIST_PATH}&search=acme&status=contacted`]: { items: [], total: 0 },
    });
    render(<ProspectsPage />);
    await screen.findByTestId("prospects-empty");

    await user.type(screen.getByLabelText("Search prospects"), "acme");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith(`${LIST_PATH}&search=acme`),
    );

    await user.selectOptions(screen.getByLabelText("Filter by status"), "contacted");
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith(`${LIST_PATH}&search=acme&status=contacted`),
    );

    // a filtered empty result says so — it is not the same claim as "no prospects at all"
    expect(screen.getByTestId("prospects-empty")).toHaveTextContent(/No prospects match/);
  });
});

describe("ProspectsPage — the add form", () => {
  it("POSTs the draft and shows the SERVER's answer (normalised), not the typed value", async () => {
    const user = userEvent.setup();
    answerWith();
    // the server normalises: trimmed + lowercased. The page must paint THIS value.
    mockPost.mockResolvedValue(prospect({ id: 21, email: "ada@example.com" }));
    render(<ProspectsPage />);
    await screen.findByTestId("prospects-empty");

    await user.type(screen.getByLabelText("Email"), "  Ada@Example.COM ");
    await user.type(screen.getByLabelText("First name"), "Ada");
    await user.type(screen.getByLabelText("Company"), "Analytical Engines");
    await user.click(screen.getByRole("button", { name: "Create prospect" }));

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith("/prospects", {
      email: "  Ada@Example.COM ",
      first_name: "Ada",
      company: "Analytical Engines",
    });

    const row = await screen.findByTestId("prospect-row-21");
    expect(within(row).getByTestId("prospect-email")).toHaveTextContent("ada@example.com");
    // the form is cleared only AFTER the server answered — the typed value is gone from the box
    await waitFor(() => expect(screen.getByLabelText("Email")).toHaveValue(""));
  });

  it("refuses a blank address locally, naming the field, and sends nothing", async () => {
    const user = userEvent.setup();
    answerWith();
    render(<ProspectsPage />);
    await screen.findByTestId("prospects-empty");

    await user.type(screen.getByLabelText("First name"), "Ada");
    await user.click(screen.getByRole("button", { name: "Create prospect" }));

    expect(mockPost).not.toHaveBeenCalled();
    expect(screen.getByTestId("prospect-create-refusal")).toHaveTextContent(/email/i);
  });

  it("renders the server's named refusal inline and keeps the draft for a retry", async () => {
    const user = userEvent.setup();
    answerWith();
    mockPost.mockRejectedValue(
      new ApiError(
        422,
        "prospect ada@example.com already exists",
        "E_VALIDATION",
        "re-use the existing prospect row",
        { field: "email" },
      ),
    );
    render(<ProspectsPage />);
    await screen.findByTestId("prospects-empty");

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.click(screen.getByRole("button", { name: "Create prospect" }));

    const alert = await screen.findByTestId("prospects-action-error");
    expect(alert).toHaveTextContent("E_VALIDATION: prospect ada@example.com already exists");
    expect(screen.getByLabelText("Email")).toHaveValue("ada@example.com");
    expect(screen.queryByTestId("prospect-row-7")).toBeNull();
  });
});

describe("ProspectsPage — inline edit and delete", () => {
  it("PATCHes the status and paints the SERVER's answer, not the requested word", async () => {
    const user = userEvent.setup();
    answerWith({ [LIST_PATH]: { items: [prospect()], total: 1 } });
    // the double answers a DIFFERENT status than the one requested — the page must show the
    // server's word, which proves it is not optimistically painting the select's value.
    mockPatch.mockResolvedValue(prospect({ status: "converted" }));
    render(<ProspectsPage />);

    const row = await screen.findByTestId("prospect-row-7");
    await user.selectOptions(within(row).getByLabelText("Status for ada@example.com"), "contacted");

    expect(mockPatch).toHaveBeenCalledWith("/prospects/7", { status: "contacted" });
    await waitFor(() =>
      expect(within(screen.getByTestId("prospect-row-7")).getByTestId("prospect-status")).toHaveTextContent(
        "converted",
      ),
    );
  });

  it("PATCHes only the fields the operator changed", async () => {
    const user = userEvent.setup();
    answerWith({ [LIST_PATH]: { items: [prospect()], total: 1 } });
    mockPatch.mockResolvedValue(prospect({ company: "New Co" }));
    render(<ProspectsPage />);

    const row = await screen.findByTestId("prospect-row-7");
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    const form = within(row).getByTestId("prospect-edit-form");
    await user.clear(within(form).getByLabelText("Company"));
    await user.type(within(form).getByLabelText("Company"), "New Co");
    await user.click(within(form).getByRole("button", { name: "Save" }));

    expect(mockPatch).toHaveBeenCalledWith("/prospects/7", { company: "New Co" });
    await waitFor(() =>
      expect(
        within(screen.getByTestId("prospect-row-7")).getByTestId("prospect-company"),
      ).toHaveTextContent("New Co"),
    );
    expect(within(screen.getByTestId("prospect-row-7")).queryByTestId("prospect-edit-form")).toBeNull();
  });

  it("deletes in TWO steps — the first press removes nothing", async () => {
    const user = userEvent.setup();
    answerWith({ [LIST_PATH]: { items: [prospect()], total: 1 } });
    mockDelete.mockResolvedValue(undefined);
    render(<ProspectsPage />);

    const row = await screen.findByTestId("prospect-row-7");
    await user.click(within(row).getByRole("button", { name: "Delete" }));

    expect(mockDelete).not.toHaveBeenCalled();
    expect(within(row).getByTestId("prospect-delete-confirm")).toBeInTheDocument();

    await user.click(within(row).getByTestId("prospect-delete-confirm"));

    expect(mockDelete).toHaveBeenCalledWith("/prospects/7");
    await waitFor(() => expect(screen.queryByTestId("prospect-row-7")).toBeNull());
    expect(screen.getByTestId("prospects-empty")).toHaveTextContent(/No prospects yet/);
  });

  it("cancelling the confirm step deletes nothing", async () => {
    const user = userEvent.setup();
    answerWith({ [LIST_PATH]: { items: [prospect()], total: 1 } });
    render(<ProspectsPage />);

    const row = await screen.findByTestId("prospect-row-7");
    await user.click(within(row).getByRole("button", { name: "Delete" }));
    await user.click(within(row).getByRole("button", { name: "Cancel" }));

    expect(mockDelete).not.toHaveBeenCalled();
    expect(within(row).queryByTestId("prospect-delete-confirm")).toBeNull();
  });
});

describe("nav table — the prospects route", () => {
  it("carries exactly one /prospects item, labelled Prospects, right after Outreach", () => {
    const items = NAV_ITEMS.filter((item) => item.href === "/prospects");
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe("Prospects");
    expect(items[0].icon).toBeDefined();

    const hrefs = NAV_ITEMS.map((item) => item.href);
    expect(hrefs[hrefs.indexOf("/outreach") + 1]).toBe("/prospects");
    // V27/L5 moved the nav count 18 -> 19 in the SAME edit that added this row.
    expect(NAV_ITEMS).toHaveLength(19);
  });
});
