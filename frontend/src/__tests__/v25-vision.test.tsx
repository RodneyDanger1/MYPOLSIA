import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * Wave V25 / L1 — vision input (frontend half). Suite `v25-vision`.
 *
 * The behaviour under test is the attachment surface: a LIST of the task's images with real
 * thumbnails, an UPLOAD control, a REMOVE control, and refusals that are rendered exactly as
 * the server named them.  The client refuses an obviously unsupported type/size BEFORE any
 * upload (`no upload on unsupported type`), and the New Task dialog understands that an image
 * can only be attached AFTER the task exists — a failed upload must not pretend the task failed.
 *
 * Mocking strategy: global `fetch` (the u7/v24 idiom), so the REAL client module runs (URL
 * building, FormData, refusal parsing).  `URL.createObjectURL` is stubbed because jsdom has no
 * Blob URL implementation; the tests assert the thumbnail's src is the URL the component created.
 */
const mockRouter = { push: jest.fn() };

jest.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
  useSearchParams: () => null,
}));

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink(props: { href: string; children: React.ReactNode }) {
    const { href, children } = props;
    return <a href={href}>{children}</a>;
  },
}));

import { NewTaskDialog } from "@/components/tasks/NewTaskDialog";
import { TaskDetailContent } from "@/components/tasks/TaskDetailContent";
import type { TaskAttachment, TaskRow } from "@/components/tasks/types";

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
  mockRouter.push.mockReset();
  createObjectURL.mockClear();
  revokeObjectURL.mockClear();
  blobSeq = 0;
  (global as unknown as { fetch: jest.Mock }).fetch = fetchMock;
});

function task(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 7,
    title: "Read the screenshot",
    agent_type: "finance",
    status: "pending",
    priority: 3,
    created_at: new Date(Date.now() - 60_000).toISOString(),
    ...overrides,
  };
}

function attachment(overrides: Partial<TaskAttachment> = {}): TaskAttachment {
  return {
    id: 11,
    task_id: 7,
    filename: "shot.png",
    mime: "image/png",
    size_bytes: 2048,
    sha256: "a".repeat(64),
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

function png(name = "shot.png", type = "image/png"): File {
  return new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3])], name, { type });
}

const isList = (url: string, id = 7) =>
  url.endsWith(`/api/v1/tasks/${id}/attachments`);
const isUpload = (url: string, init: RequestInit | undefined, id = 7) =>
  isList(url, id) && (init?.method ?? "GET").toUpperCase() === "POST";

// ─────────────────────────────────────────────────────────────────────────────────────────────
// The detail surface: list + thumbnails + upload + remove
// ─────────────────────────────────────────────────────────────────────────────────────────────

describe("task images — the detail surface", () => {
  it("lists the task's images with authenticated blob thumbnails", async () => {
    const row = attachment();
    fetchMock.mockImplementation((url: string) => {
      if (isList(url) && !String(url).includes("/attachments/")) {
        return Promise.resolve(jsonResponse(200, [row]));
      }
      if (url.endsWith("/api/v1/attachments/11")) {
        return Promise.resolve(blobResponse(new Uint8Array([1, 2, 3]), "image/png"));
      }
      return Promise.resolve(jsonResponse(404, { detail: { code: "E_NOT_FOUND", message: "no" } }));
    });

    render(<TaskDetailContent task={task()} />);

    const image = await screen.findByAltText("shot.png");
    expect(image).toHaveAttribute("src", "blob:mock-1");
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).endsWith("/api/v1/tasks/7/attachments")),
    ).toBe(true);
  });

  it("uploads the chosen file as multipart to the exact route and refreshes the list", async () => {
    const user = userEvent.setup();
    const created = attachment({ id: 12, filename: "chart.png" });
    let listed = 0;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (isUpload(url, init)) {
        return Promise.resolve(jsonResponse(201, created));
      }
      if (isList(url)) {
        listed += 1;
        return Promise.resolve(jsonResponse(200, listed === 1 ? [] : [created]));
      }
      if (String(url).includes("/api/v1/attachments/")) {
        return Promise.resolve(blobResponse(new Uint8Array([1]), "image/png"));
      }
      return Promise.resolve(jsonResponse(404, {}));
    });
    const onChanged = jest.fn();

    render(<TaskDetailContent task={task()} onTaskQueued={onChanged} />);
    await screen.findByText("No images attached to this task.");

    const input = screen.getByLabelText(/attach an image$/i);
    expect(input).toHaveAttribute("accept", "image/png,image/jpeg,image/webp");
    await user.upload(input, png("chart.png"));

    const upload = await screen.findByAltText("chart.png");
    expect(upload).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent('"chart.png" attached.');

    const call = fetchMock.mock.calls.find(([url, init]) => isUpload(url, init));
    expect(call).toBeDefined();
    const [url, init] = call as [string, RequestInit];
    expect(url).toMatch(/\/api\/v1\/tasks\/7\/attachments$/);
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    const form = init.body as FormData;
    expect((form.get("file") as File).name).toBe("chart.png");
    const headers = new Map(Object.entries((init.headers ?? {}) as Record<string, string>).map(([k, v]) => [k.toLowerCase(), v]));
    expect(headers.has("content-type")).toBe(false);
    expect(onChanged).toHaveBeenCalled();
  });

  it("refuses an unsupported type on the client and uploads NOTHING", async () => {
    const user = userEvent.setup({ applyAccept: false });
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    render(<TaskDetailContent task={task()} />);
    await screen.findByText("No images attached to this task.");
    const before = fetchMock.mock.calls.length;

    await user.upload(screen.getByLabelText(/attach an image$/i), png("notes.gif", "image/gif"));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("not a PNG, JPEG or WebP");
    expect(alert).toHaveTextContent("nothing was uploaded");
    expect(fetchMock.mock.calls.length).toBe(before);
    expect(fetchMock.mock.calls.some(([url, init]) => isUpload(url, init))).toBe(false);
  });

  it("refuses a file over 5 MB on the client and uploads NOTHING", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    render(<TaskDetailContent task={task()} />);
    await screen.findByText("No images attached to this task.");
    const big = png("huge.png");
    Object.defineProperty(big, "size", { value: 5 * 1024 * 1024 + 1 });
    const before = fetchMock.mock.calls.length;

    await user.upload(screen.getByLabelText(/attach an image$/i), big);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("limit is 5 MB");
    expect(alert).toHaveTextContent("nothing was uploaded");
    expect(fetchMock.mock.calls.length).toBe(before);
  });

  it("renders a server refusal verbatim (code + how_to_fix) after an upload attempt", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (isUpload(url, init)) {
        return Promise.resolve(
          jsonResponse(422, {
            detail: {
              code: "E_VALIDATION",
              message: "attachment upload: the file is not PNG, JPEG or WebP (the bytes were inspected)",
              how_to_fix: "upload a PNG, JPEG or WebP image; other formats are not accepted",
              context: { filename: "fake.png" },
            },
          }),
        );
      }
      return Promise.resolve(jsonResponse(200, []));
    });

    render(<TaskDetailContent task={task()} />);
    await screen.findByText("No images attached to this task.");
    await user.upload(screen.getByLabelText(/attach an image$/i), png("fake.png"));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_VALIDATION:");
    expect(alert).toHaveTextContent("other formats are not accepted");
  });

  it("removes an image by id and says so", async () => {
    const user = userEvent.setup();
    const row = attachment();
    let listed = 0;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "DELETE" && url.endsWith("/api/v1/attachments/11")) {
        return Promise.resolve({
          ok: true, status: 204, statusText: "No Content",
          headers: { get: () => null }, text: async () => "", json: async () => ({}),
          blob: async () => new Blob([]),
        } as FetchLikeResponse);
      }
      if (isList(url)) {
        listed += 1;
        return Promise.resolve(jsonResponse(200, listed === 1 ? [row] : []));
      }
      return Promise.resolve(blobResponse(new Uint8Array([1]), "image/png"));
    });

    render(<TaskDetailContent task={task()} />);
    await screen.findByAltText("shot.png");

    await user.click(screen.getByRole("button", { name: "Remove shot.png" }));

    expect(await screen.findByText("No images attached to this task.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent('"shot.png" removed.');
    const del = fetchMock.mock.calls.find(([, init]) => init?.method === "DELETE");
    expect(String(del?.[0])).toMatch(/\/api\/v1\/attachments\/11$/);
  });

  it("keeps existing thumbnails alive across a list refresh (no revoked URL still on screen)", async () => {
    const user = userEvent.setup();
    const first = attachment({ id: 21, filename: "keep.png" });
    const second = attachment({ id: 22, filename: "drop.png" });
    let listed = 0;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "DELETE" && url.endsWith("/api/v1/attachments/22")) {
        return Promise.resolve({
          ok: true, status: 204, statusText: "No Content",
          headers: { get: () => null }, text: async () => "", json: async () => ({}),
          blob: async () => new Blob([]),
        } as FetchLikeResponse);
      }
      if (isList(url)) {
        listed += 1;
        return Promise.resolve(jsonResponse(200, listed === 1 ? [first, second] : [first]));
      }
      return Promise.resolve(blobResponse(new Uint8Array([1]), "image/png"));
    });

    render(<TaskDetailContent task={task()} />);
    const kept = await screen.findByAltText("keep.png");
    expect(kept).toHaveAttribute("src", "blob:mock-1");
    await screen.findByAltText("drop.png");

    await user.click(screen.getByRole("button", { name: "Remove drop.png" }));

    await waitFor(() => expect(screen.queryByAltText("drop.png")).toBeNull());
    // The row LIST refetched; the surviving row's blob URL must still be rendered, not revoked.
    expect(screen.getByAltText("keep.png")).toHaveAttribute("src", "blob:mock-1");
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });

  it("states the 4-per-task limit and disables the picker when it is reached", async () => {
    const rows = Array.from({ length: 4 }, (_, i) =>
      attachment({ id: 20 + i, filename: `image-${i}.png` }),
    );
    fetchMock.mockImplementation((url: string) => {
      if (isList(url)) return Promise.resolve(jsonResponse(200, rows));
      return Promise.resolve(blobResponse(new Uint8Array([1]), "image/png"));
    });

    render(<TaskDetailContent task={task()} />);
    await screen.findAllByAltText(/image-\d\.png/);

    expect(screen.getByLabelText(/attach an image$/i)).toBeDisabled();
    expect(screen.getByText(/4 of 4 images attached/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Remove image-/ })).toHaveLength(4);
  });
});

// ─────────────────────────────────────────────────────────────────────────────────────────────
// The New Task dialog: pick before create, upload after the 201
// ─────────────────────────────────────────────────────────────────────────────────────────────

async function fillRequired(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/title \(required\)/i), "Read this chart");
  await user.selectOptions(screen.getByLabelText(/agent \(required\)/i), "finance");
}

describe("new task dialog — images", () => {
  it("holds the image until the task exists, then uploads it to the new id", async () => {
    const user = userEvent.setup();
    const created = task({ id: 42, title: "Read this chart" });
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/v1/tasks") && (init?.method ?? "") === "POST") {
        return Promise.resolve(jsonResponse(201, created));
      }
      if (isList(url, 42) && (init?.method ?? "") === "POST") {
        return Promise.resolve(jsonResponse(201, attachment({ id: 99, task_id: 42 })));
      }
      return Promise.resolve(jsonResponse(404, {}));
    });
    const onCreated = jest.fn();

    render(<NewTaskDialog onClose={jest.fn()} onCreated={onCreated} />);
    await user.upload(screen.getByLabelText(/attach images to the new task/i), png("chart.png"));
    expect(screen.getByAltText("chart.png")).toBeInTheDocument();

    await fillRequired(user);
    await user.click(screen.getByRole("button", { name: /create task/i }));

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Task #42 created with 1 image");
    expect(notice).toHaveTextContent("waits in the queue");
    expect(onCreated).toHaveBeenCalledWith(created);
    const upload = fetchMock.mock.calls.find(([url, init]) => isList(url, 42) && init?.method === "POST");
    expect(upload).toBeDefined();
    // the task was created BEFORE the image was attached
    const createIndex = fetchMock.mock.calls.findIndex(([url, init]) => url.endsWith("/api/v1/tasks") && init?.method === "POST");
    const uploadIndex = fetchMock.mock.calls.findIndex(([url, init]) => isList(url, 42) && init?.method === "POST");
    expect(createIndex).toBeGreaterThanOrEqual(0);
    expect(uploadIndex).toBeGreaterThan(createIndex);
  });

  it("reports a failed attachment honestly — the task still exists", async () => {
    const user = userEvent.setup();
    const created = task({ id: 43, title: "Read this chart" });
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/v1/tasks") && init?.method === "POST") {
        return Promise.resolve(jsonResponse(201, created));
      }
      if (isList(url, 43) && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(422, {
            detail: {
              code: "E_VALIDATION",
              message: "attachment upload: the bytes are not PNG, JPEG or WebP",
              how_to_fix: "upload a PNG, JPEG or WebP image",
            },
          }),
        );
      }
      return Promise.resolve(jsonResponse(404, {}));
    });

    render(<NewTaskDialog onClose={jest.fn()} />);
    await user.upload(screen.getByLabelText(/attach images to the new task/i), png("chart.png"));
    await fillRequired(user);
    await user.click(screen.getByRole("button", { name: /create task/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Task #43 was created");
    expect(alert).toHaveTextContent("NOT attached");
    expect(alert).toHaveTextContent("E_VALIDATION:");
    expect(alert).toHaveTextContent("Open the task to attach them again.");
  });

  it("never adds an unsupported file to the pending list", async () => {
    const user = userEvent.setup({ applyAccept: false });
    render(<NewTaskDialog onClose={jest.fn()} />);

    await user.upload(screen.getByLabelText(/attach images to the new task/i), png("x.gif", "image/gif"));

    expect(await screen.findByRole("alert")).toHaveTextContent("not a PNG, JPEG or WebP");
    expect(screen.queryByRole("list", { name: /images to attach/i })).toBeNull();
    expect(screen.queryByAltText("x.gif")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("removes a pending image before the task is created (and revokes its preview URL)", async () => {
    const user = userEvent.setup();
    render(<NewTaskDialog onClose={jest.fn()} />);

    await user.upload(screen.getByLabelText(/attach images to the new task/i), png("chart.png"));
    expect(screen.getByAltText("chart.png")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Remove chart.png from the new task" }));
    expect(screen.queryByAltText("chart.png")).toBeNull();
    expect(revokeObjectURL).toHaveBeenCalled();
  });
});
