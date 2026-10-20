import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * Wave V27 / L4 — vision memory (frontend half).  Suite `v27-vision-memory`.
 *
 * The behaviour under test is the **previous descriptions** block that V27 adds to the
 * attachment Describe panel, plus the `cached` provenance bit the describe answer now carries:
 *
 *   * the block reads `GET /api/v1/vision/descriptions?task_id=<task>` (the REAL client module,
 *     so URL building, the owner key header and refusal parsing are all exercised);
 *   * rows render newest-first as the server sent them, each with its model, measured tokens
 *     (or "tokens unmeasured"), the sha256 PREFIX and the stored date;
 *   * an empty history is the honest sentence "No previous descriptions for this task." — never
 *     a spinner that never resolves and never a fabricated row;
 *   * a history refusal shows the server's named code (`E_NOT_FOUND`) and is NEVER rendered as
 *     "no previous descriptions" (a failed read is not an empty read);
 *   * pressing Describe POSTs exactly `{"attachment_id": <id>}`, renders the sentence, and a
 *     `cached: true` answer says so ("Served from memory — no new model call."); afterwards the
 *     history is refetched so it cannot show a stale list beside a fresh answer;
 *   * a describe refusal clears the busy state and leaves the history block intact;
 *   * without `taskId` the component is exactly what V26 shipped: no history request at all.
 *
 * Mocking strategy: global `fetch` with a URL router (the v25/v26 idiom), so the real client
 * module runs.  The backend is a labelled HTTP double — no product code branches on it.
 */
type FetchLikeResponse = {
  ok: boolean;
  status: number;
  statusText: string;
  headers: { get: (name: string) => string | null };
  text: () => Promise<string>;
  json: () => Promise<unknown>;
};

function jsonResponse(status: number, body: unknown): FetchLikeResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "mock status",
    headers: { get: () => null },
    text: async () => JSON.stringify(body),
    json: async () => body,
  };
}

function refusal(status: number, code: string, message: string, howToFix = ""): FetchLikeResponse {
  return jsonResponse(status, {
    ok: false,
    error: { code, message, how_to_fix: howToFix, context: {} },
  });
}

const TASK_ID = 7;
const ATTACHMENT_ID = 11;

const fetchMock = jest.fn();

beforeEach(() => {
  fetchMock.mockReset();
  (global as unknown as { fetch: jest.Mock }).fetch = fetchMock;
});

const isDescribe = (url: string) => url.endsWith("/api/v1/vision/describe");
const isHistory = (url: string) => url.includes("/api/v1/vision/descriptions");

type Route = (init: RequestInit | undefined) => FetchLikeResponse | Promise<FetchLikeResponse>;

function historyRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    attachment_id: ATTACHMENT_ID,
    sha256_prefix: "a1b2c3d4e5f6",
    description: "The image is a solid red square.",
    model: "deepseek-flash",
    tokens: 138,
    created_at: "2026-10-19T09:30:00Z",
    ...overrides,
  };
}

/** URL router: history (GET) and describe (POST).  Anything else is a hard failure.
 *
 * `history` is a route function, or a list of RESPONSE BODIES answered 200 in order (the last
 * one repeats) — enough to model "empty, then the row we just described".
 */
function installRoutes({
  history,
  describe,
}: {
  history: Route | Array<unknown>;
  describe: Route;
}): void {
  let historyCalls = 0;
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (isHistory(url) && method === "GET") {
      if (Array.isArray(history)) {
        const body = history[Math.min(historyCalls, history.length - 1)];
        historyCalls += 1;
        return jsonResponse(200, body);
      }
      historyCalls += 1;
      return history(init);
    }
    if (isDescribe(url) && method === "POST") return describe(init);
    throw new Error(`unexpected fetch ${method} ${url}`);
  });
}

function fetchCalls(kind: "describe" | "history"): Array<{ url: string; init: RequestInit }> {
  const match = kind === "describe" ? isDescribe : isHistory;
  return fetchMock.mock.calls
    .filter(([url]) => match(url as string))
    .map(([url, init]) => ({ url: url as string, init: (init ?? {}) as RequestInit }));
}

async function renderPanel(taskId: number | null = TASK_ID): Promise<void> {
  const { AttachmentDescribe } = await import("@/components/vision/AttachmentDescribe");
  render(
    <AttachmentDescribe
      attachmentId={ATTACHMENT_ID}
      filename="shot.png"
      {...(taskId === null ? {} : { taskId })}
    />,
  );
  await screen.findByRole("button", { name: "Describe shot.png" });
}

describe("V27/L4 — previous descriptions", () => {
  it("reads the task history and renders the rows newest-first with model, tokens and sha prefix", async () => {
    installRoutes({
      history: [
        [
          historyRow({ id: 2, description: "Second look.", tokens: 12, sha256_prefix: "bbbbbbbbbbbb" }),
          historyRow({ id: 1, description: "First look.", tokens: 138 }),
        ],
      ],
      describe: () => jsonResponse(200, {}),
    });

    await renderPanel();

    const block = await screen.findByTestId(`vision-history-${TASK_ID}`);
    await waitFor(() => expect(block).toHaveTextContent("Second look."));

    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Second look.");
    expect(items[1]).toHaveTextContent("First look.");
    expect(items[0]).toHaveTextContent("12 tokens");
    expect(items[1]).toHaveTextContent("138 tokens");
    expect(items[1]).toHaveTextContent("sha a1b2c3d4e5f6");
    expect(items[1]).toHaveTextContent("2026-10-19");
    expect(items[1]).toHaveTextContent("deepseek-flash");
    expect(block).toHaveTextContent("Previous descriptions (2)");

    const calls = fetchCalls("history");
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain(`/api/v1/vision/descriptions?task_id=${TASK_ID}`);
    expect(calls[0].init.method ?? "GET").toBe("GET");
  });

  it("renders an honest empty state when the task has no stored descriptions", async () => {
    installRoutes({ history: () => jsonResponse(200, []), describe: () => jsonResponse(200, {}) });

    await renderPanel();

    const block = await screen.findByTestId(`vision-history-${TASK_ID}`);
    await waitFor(() =>
      expect(block).toHaveTextContent("No previous descriptions for this task."),
    );
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  it("renders a history refusal as the server's named code, never as an empty history", async () => {
    installRoutes({
      history: () =>
        refusal(404, "E_NOT_FOUND", "vision history: task 7 does not exist", "check the id"),
      describe: () => jsonResponse(200, {}),
    });

    await renderPanel();

    const alert = await screen.findByTestId(`vision-history-error-${TASK_ID}`);
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_NOT_FOUND");
    expect(
      screen.queryByText("No previous descriptions for this task."),
    ).not.toBeInTheDocument();
  });

  it("marks a cached answer and refetches the history after a describe", async () => {
    const user = userEvent.setup();
    installRoutes({
      history: [
        [],
        [historyRow({ id: 9, description: "Served again.", tokens: 138 })],
      ],
      describe: () =>
        jsonResponse(200, {
          description: "Served again.",
          model: "deepseek-flash",
          tokens: 138,
          cached: true,
        }),
    });

    await renderPanel();
    const block = await screen.findByTestId(`vision-history-${TASK_ID}`);
    await waitFor(() =>
      expect(block).toHaveTextContent("No previous descriptions for this task."),
    );

    await user.click(screen.getByRole("button", { name: "Describe shot.png" }));

    const answer = await screen.findByTestId(`vision-description-${ATTACHMENT_ID}`);
    expect(answer).toHaveTextContent("Served again.");
    expect(answer).toHaveTextContent("Served from memory — no new model call.");
    expect(answer).toHaveTextContent("138 tokens");

    await waitFor(() => expect(block).toHaveTextContent("Served again."));
    expect(fetchCalls("history")).toHaveLength(2);

    const posts = fetchCalls("describe");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0].init.body))).toEqual({ attachment_id: ATTACHMENT_ID });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Describe shot.png" })).not.toBeDisabled(),
    );
  });

  it("says nothing about memory for a freshly measured answer and clears busy on a refusal", async () => {
    const user = userEvent.setup();
    installRoutes({
      history: () => jsonResponse(200, [historyRow()]),
      describe: () =>
        refusal(503, "E_CONFIG", "vision describe: no DeepSeek API key is configured", "set it"),
    });

    await renderPanel();
    const block = await screen.findByTestId(`vision-history-${TASK_ID}`);
    await waitFor(() => expect(block).toHaveTextContent("The image is a solid red square."));

    await user.click(screen.getByRole("button", { name: "Describe shot.png" }));

    const alert = await screen.findByTestId(`vision-error-${ATTACHMENT_ID}`);
    expect(alert).toHaveTextContent("E_CONFIG");
    expect(screen.queryByTestId(`vision-description-${ATTACHMENT_ID}`)).not.toBeInTheDocument();
    // The history is a separate read: a refused describe does not erase what was already stored.
    expect(block).toHaveTextContent("The image is a solid red square.");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Describe shot.png" })).not.toBeDisabled(),
    );
  });

  it("shows unmeasured tokens honestly and never invents a zero", async () => {
    installRoutes({
      history: () => jsonResponse(200, [historyRow({ tokens: null })]),
      describe: () => jsonResponse(200, {}),
    });

    await renderPanel();

    const block = await screen.findByTestId(`vision-history-${TASK_ID}`);
    await waitFor(() => expect(block).toHaveTextContent("tokens unmeasured"));
    expect(block).not.toHaveTextContent("0 tokens");
  });

  it("does not read history at all when no taskId is passed (the V26 surface is unchanged)", async () => {
    installRoutes({ history: () => jsonResponse(200, []), describe: () => jsonResponse(200, {}) });

    await renderPanel(null);

    expect(screen.queryByTestId(`vision-history-${TASK_ID}`)).not.toBeInTheDocument();
    expect(fetchCalls("history")).toHaveLength(0);
    expect(fetchCalls("describe")).toHaveLength(0);
  });
});
