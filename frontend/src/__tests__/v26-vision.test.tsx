import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * Wave V26 / L4 — vision on demand (frontend half). Suite `v26-vision`.
 *
 * The behaviour under test is the **Describe** action on one attachment:
 *
 *   * pressing it POSTs exactly `{"attachment_id": <id>}` to `/api/v1/vision/describe`
 *     (the same client module the UI ships, so URL building / headers / refusal parsing are
 *     all real);
 *   * the answer renders INLINE in a block that says it is model output and is not saved to
 *     the task, with the served model and the measured token count;
 *   * `tokens: null` renders "tokens unmeasured", never a fabricated `0`;
 *   * `E_CONFIG` / `E_UPSTREAM` render the server's named refusal (code + message + fix hint)
 *     and NO description;
 *   * the button's busy state always clears, and describing never uploads, deletes or patches
 *     the task's result.
 *
 * Mocking strategy: global `fetch` (the v25/u7 idiom) with a URL router, so the REAL client
 * module runs.  `URL.createObjectURL` is stubbed because jsdom has no Blob URL implementation.
 * The model call itself is a labelled HTTP double — no product code branches on it.
 */
type FetchLikeResponse = {
  ok: boolean;
  status: number;
  statusText: string;
  headers: { get: (name: string) => string | null };
  text: () => Promise<string>;
  json: () => Promise<unknown>;
  blob: () => Promise<Blob>;
};

function jsonResponse(status: number, body: unknown): FetchLikeResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "mock status",
    headers: { get: () => null },
    text: async () => JSON.stringify(body),
    json: async () => body,
    blob: async () => new Blob([JSON.stringify(body)]),
  };
}

function blobResponse(bytes: Uint8Array, type: string): FetchLikeResponse {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: { get: () => null },
    text: async () => "",
    json: async () => ({}),
    blob: async () => new Blob([bytes.slice().buffer as ArrayBuffer], { type }),
  };
}

function refusal(status: number, code: string, message: string, howToFix = ""): FetchLikeResponse {
  return jsonResponse(status, {
    ok: false,
    error: { code, message, how_to_fix: howToFix, context: {} },
  });
}

const PNG_BYTES = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);

const fetchMock = jest.fn();
let blobSeq = 0;
const createObjectURL = jest.fn(() => `blob:mock-${++blobSeq}`);
const revokeObjectURL = jest.fn();

beforeAll(() => {
  Object.defineProperty(URL, "createObjectURL", { writable: true, value: createObjectURL });
  Object.defineProperty(URL, "revokeObjectURL", { writable: true, value: revokeObjectURL });
});

beforeEach(() => {
  fetchMock.mockReset();
  createObjectURL.mockClear();
  revokeObjectURL.mockClear();
  blobSeq = 0;
  (global as unknown as { fetch: jest.Mock }).fetch = fetchMock;
});

const isList = (url: string) => url.endsWith("/api/v1/tasks/7/attachments");
const isBlob = (url: string) => url.endsWith("/api/v1/attachments/11");
const isDescribe = (url: string) => url.endsWith("/api/v1/vision/describe");

type Route = (init: RequestInit | undefined) => FetchLikeResponse | Promise<FetchLikeResponse>;

/** URL router: the three reads the component performs, plus the describe call under test. */
function installRoutes(describe: Route): void {
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (isList(url) && method === "GET") {
      return jsonResponse(200, [
        {
          id: 11,
          task_id: 7,
          filename: "shot.png",
          mime: "image/png",
          size_bytes: PNG_BYTES.length,
          sha256: "a".repeat(64),
          created_at: new Date().toISOString(),
        },
      ]);
    }
    if (isBlob(url) && method === "GET") return blobResponse(PNG_BYTES, "image/png");
    if (isDescribe(url) && method === "POST") return describe(init);
    throw new Error(`unexpected fetch ${method} ${url}`);
  });
}

function describeCalls(): Array<{ url: string; init: RequestInit }> {
  return fetchMock.mock.calls
    .filter(([url]) => isDescribe(url as string))
    .map(([url, init]) => ({ url: url as string, init: (init ?? {}) as RequestInit }));
}

function writeCalls(): string[] {
  return fetchMock.mock.calls
    .map(([url, init]) => `${((init as RequestInit | undefined)?.method ?? "GET").toUpperCase()} ${url}`)
    .filter((entry) => !entry.startsWith("GET ") && !entry.includes("/api/v1/vision/describe"));
}

async function renderAttachments(): Promise<void> {
  const { TaskAttachments } = await import("@/components/tasks/TaskAttachments");
  render(<TaskAttachments taskId={7} />);
  await screen.findByRole("button", { name: "Remove shot.png" });
}

describe("V26/L4 — Describe an attachment", () => {
  it("posts the attachment id and renders the model sentence inline, labelled as model output", async () => {
    const user = userEvent.setup();
    installRoutes(() =>
      jsonResponse(200, {
        description: "The image is a solid red square.",
        model: "deepseek-flash",
        tokens: 138,
      }),
    );

    await renderAttachments();
    await user.click(screen.getByRole("button", { name: "Describe shot.png" }));

    const block = await screen.findByTestId("vision-description-11");
    expect(block).toHaveTextContent("Model description — not saved to the task");
    expect(block).toHaveTextContent("The image is a solid red square.");
    expect(block).toHaveTextContent("Model: deepseek-flash");
    expect(block).toHaveTextContent("138 tokens");

    const calls = describeCalls();
    expect(calls).toHaveLength(1);
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ attachment_id: 11 });
    expect((calls[0].init.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );

    // Describing is a READ of the model over one image: no upload, no delete, no task write.
    expect(writeCalls()).toEqual([]);

    // And the busy state clears: the control is a plain Describe again.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Describe shot.png" })).not.toBeDisabled(),
    );
  });

  it("renders unmeasured usage as 'tokens unmeasured', never a fabricated zero", async () => {
    const user = userEvent.setup();
    installRoutes(() =>
      jsonResponse(200, { description: "A red square.", model: "deepseek-flash", tokens: null }),
    );

    await renderAttachments();
    await user.click(screen.getByRole("button", { name: "Describe shot.png" }));

    const block = await screen.findByTestId("vision-description-11");
    expect(block).toHaveTextContent("tokens unmeasured");
    expect(block).not.toHaveTextContent("0 tokens");
  });

  it("shows E_CONFIG exactly as the server named it and renders no description", async () => {
    const user = userEvent.setup();
    installRoutes(() =>
      refusal(
        503,
        "E_CONFIG",
        "vision describe: no DeepSeek API key is configured (set DEEPSEEK_API_KEY or save one in Settings)",
        "set the variable in .env (see .env.example) and restart the service",
      ),
    );

    await renderAttachments();
    await user.click(screen.getByRole("button", { name: "Describe shot.png" }));

    const alert = await screen.findByTestId("vision-error-11");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_CONFIG");
    expect(alert).toHaveTextContent("DEEPSEEK_API_KEY");
    expect(screen.queryByTestId("vision-description-11")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Describe shot.png" })).not.toBeDisabled(),
    );
  });

  it("shows E_UPSTREAM with the provider status and clears the busy state", async () => {
    const user = userEvent.setup();
    installRoutes(() =>
      refusal(
        502,
        "E_UPSTREAM",
        "vision describe: DeepSeek returned an error (error_api_error, provider status 500)",
        "check the provider's status page and retry",
      ),
    );

    await renderAttachments();
    await user.click(screen.getByRole("button", { name: "Describe shot.png" }));

    const alert = await screen.findByTestId("vision-error-11");
    expect(alert).toHaveTextContent("E_UPSTREAM");
    expect(alert).toHaveTextContent("500");
    expect(screen.queryByTestId("vision-description-11")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Describe shot.png" })).not.toBeDisabled(),
    );
  });

  it("refuses a 200 with no sentence instead of rendering an empty description", async () => {
    const user = userEvent.setup();
    installRoutes(() => jsonResponse(200, { description: "   ", model: "deepseek-flash", tokens: 1 }));

    await renderAttachments();
    await user.click(screen.getByRole("button", { name: "Describe shot.png" }));

    const alert = await screen.findByTestId("vision-error-11");
    expect(alert).toHaveTextContent("E_UPSTREAM");
    expect(screen.queryByTestId("vision-description-11")).not.toBeInTheDocument();
  });
});
