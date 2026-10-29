/**
 * WAVE 3 / R-W2-B3 — the artifact UI on `/ops` (F-08/F-15's screen half).
 *
 * The wave-2 API (backend/app/api/v1/artifacts.py) serves run workspaces, their files, a file's
 * text and its exact bytes. These tests pin the states the panel must keep DISTINCT, because the
 * whole point of the surface is that a claimed-but-missing deliverable becomes visible:
 *
 *   : index loading / named read failure with Retry / empty ("no run has written a file");
 *   : run 301 = `files: 0` renders "this run wrote nothing" — the measured zero, not a spinner;
 *   : expanding run 314 lists its files, and opening one shows the text WITH the sha256 + bytes
 *     provenance the server sent in headers;
 *   : a run with no workspace is the server's `E_NOT_FOUND`, named as "no workspace for this run";
 *   : a refused content read (E_VALIDATION) renders the named refusal, never an empty preview;
 *   : Download fetches the byte route and announces what was saved;
 *   : nothing here writes, uploads or deletes — the panel only reads.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { ...actual.api, get: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { OpsArtifacts } from "@/components/ops/OpsArtifacts";

const mockGet = api.get as jest.Mock;

const INDEX = {
  items: [
    {
      run_id: "314",
      files: 2,
      bytes: 11373,
      latest_modified_at: "2026-10-28T21:12:08Z",
      truncated: false,
    },
    {
      run_id: "301",
      files: 0,
      bytes: 0,
      latest_modified_at: null,
      truncated: false,
    },
  ],
  total: 2,
  truncated: false,
  skipped: [],
};

const LISTING_314 = {
  run_id: "314",
  items: [
    { path: "notes/315-brand-one-pager.md", bytes: 4318, modified_at: "2026-10-28T21:12:08Z" },
    { path: "notes/315-label-data-checklist.md", bytes: 7055, modified_at: "2026-10-28T21:12:09Z" },
  ],
  total: 2,
  total_bytes: 11373,
  truncated: false,
  skipped_links: 0,
};

const LISTING_301 = {
  run_id: "301",
  items: [],
  total: 0,
  total_bytes: 0,
  truncated: false,
  skipped_links: 0,
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers(),
  } as unknown as Response;
}

function contentResponse(text: string, sha256: string, bytes: number): Response {
  const headers = new Headers({
    "X-Polsia-Artifact-Sha256": sha256,
    "X-Polsia-Artifact-Bytes": String(bytes),
    "X-Polsia-Artifact-Modified": "2026-10-28T21:12:08Z",
  });
  return {
    ok: true,
    status: 200,
    text: async () => text,
    headers,
  } as unknown as Response;
}

function refusalResponse(code: string, message: string, howToFix: string): Response {
  const body = { ok: false, error: { code, message, how_to_fix: howToFix, context: {} } };
  return {
    ok: false,
    status: 422,
    text: async () => JSON.stringify(body),
    headers: new Headers(),
  } as unknown as Response;
}

beforeEach(() => {
  mockGet.mockReset();
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: jest.fn(() => "blob:artifact"),
  });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: jest.fn() });
  jest.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe("R-W2-B3 — the artifact index", () => {
  it("lists the run workspaces, including the run that wrote nothing", async () => {
    mockGet.mockResolvedValue(INDEX);
    render(<OpsArtifacts />);

    expect(await screen.findByTestId("ops-artifacts-list")).toBeInTheDocument();
    const run314 = screen.getByTestId("ops-artifact-run-314");
    expect(run314).toHaveTextContent("#314");
    expect(run314).toHaveTextContent("2 FILES");
    expect(within(run314).getByText("11.1 KB")).toBeInTheDocument();

    const run301 = screen.getByTestId("ops-artifact-run-301");
    expect(run301).toHaveTextContent("0 FILES");
    expect(screen.getByTestId("ops-artifact-none-301")).toHaveTextContent(
      /This run wrote nothing/,
    );
    expect(screen.getByTestId("ops-artifacts-count")).toHaveTextContent("Showing all 2");
  });

  it("an empty index is a measured zero, not an error", async () => {
    mockGet.mockResolvedValue({ items: [], total: 0, truncated: false, skipped: [] });
    render(<OpsArtifacts />);

    expect(await screen.findByTestId("ops-artifacts-empty")).toHaveTextContent(
      /no run has written a file/,
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a failed index read is named and Retry re-issues exactly that read", async () => {
    let reads = 0;
    mockGet.mockImplementation(() => {
      reads += 1;
      return reads === 1
        ? Promise.reject(new ApiError(503, "the workspace volume is unreachable", "E_STORAGE"))
        : Promise.resolve(INDEX);
    });
    render(<OpsArtifacts />);

    expect(await screen.findByText(/Could not load the run artifact index/)).toBeInTheDocument();
    expect(screen.queryByTestId("ops-artifacts-list")).toBeNull();

    await userEvent.setup().click(screen.getByTestId("ops-artifacts-retry"));
    await waitFor(() => expect(reads).toBe(2));
    expect(await screen.findByTestId("ops-artifacts-list")).toBeInTheDocument();
  });
});

describe("R-W2-B3 — opening a run and reading its files", () => {
  it("expands run 314, lists two files and shows a file's text with its provenance", async () => {
    const user = userEvent.setup();
    mockGet.mockImplementation((path: string) => {
      if (path === "/artifacts") return Promise.resolve(INDEX);
      if (path === "/artifacts/314") return Promise.resolve(LISTING_314);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    global.fetch = jest.fn().mockResolvedValue(
      contentResponse("# Brand one-pager\n\nBody.", "22fcfe35c5e2", 4318),
    ) as unknown as typeof fetch;

    render(<OpsArtifacts />);
    await screen.findByTestId("ops-artifact-run-314");
    await user.click(screen.getByTestId("ops-artifact-run-314"));

    const fileButton = await screen.findByTestId("ops-artifact-file-314-0");
    expect(fileButton).toHaveTextContent("notes/315-brand-one-pager.md");
    await user.click(fileButton);

    const pre = await screen.findByTestId("ops-artifact-content");
    expect(pre).toHaveTextContent("# Brand one-pager");
    expect(screen.getByTestId("ops-artifact-provenance")).toHaveTextContent("22fcfe35c5e2");
    expect(screen.getByTestId("ops-artifact-provenance")).toHaveTextContent("4.2 KB");

    // The byte read went to the content route (never a bare <a href>), carrying the owner key
    // exactly when one is configured — the same rule the shared API client follows.
    const call = (global.fetch as jest.Mock).mock.calls[0];
    expect(call[0]).toContain("/api/v1/artifacts/314/content?path=notes%2F315-brand-one-pager.md");
    const sent = (call[1].headers ?? {}) as Record<string, string>;
    expect(sent["X-API-Key"] ?? "").toBe(process.env.NEXT_PUBLIC_API_KEY ?? "");
  });

  it("an existing-but-empty run workspace says it wrote nothing", async () => {
    const user = userEvent.setup();
    mockGet.mockImplementation((path: string) => {
      if (path === "/artifacts") return Promise.resolve({ ...INDEX, items: [INDEX.items[1]], total: 1 });
      if (path === "/artifacts/301") return Promise.resolve(LISTING_301);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<OpsArtifacts />);
    await screen.findByTestId("ops-artifact-run-301");
    await user.click(screen.getByTestId("ops-artifact-run-301"));

    expect(await screen.findByTestId("ops-artifact-empty-301")).toHaveTextContent(
      /this run wrote nothing/,
    );
  });

  it("a run with no workspace is the server's E_NOT_FOUND, named as such", async () => {
    const user = userEvent.setup();
    mockGet.mockImplementation((path: string) => {
      if (path === "/artifacts")
        return Promise.resolve({
          ...INDEX,
          items: [{ ...INDEX.items[0], run_id: "999" }],
          total: 1,
        });
      if (path === "/artifacts/999")
        return Promise.reject(
          new ApiError(404, "run 999 has no workspace — nothing was written for it", "E_NOT_FOUND"),
        );
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<OpsArtifacts />);
    await screen.findByTestId("ops-artifact-run-999");
    await user.click(screen.getByTestId("ops-artifact-run-999"));

    const error = await screen.findByTestId("ops-artifact-error-999");
    expect(error).toHaveTextContent(/no workspace for this run/);
    expect(error).toHaveTextContent("E_NOT_FOUND");
  });
});

describe("R-W2-B3 — a refused read and a real download", () => {
  it("renders the named refusal when the content route refuses (no empty preview)", async () => {
    const user = userEvent.setup();
    mockGet.mockImplementation((path: string) => {
      if (path === "/artifacts") return Promise.resolve(INDEX);
      if (path === "/artifacts/314") return Promise.resolve(LISTING_314);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    global.fetch = jest
      .fn()
      .mockResolvedValue(
        refusalResponse("E_VALIDATION", "'..' path traversal is refused", "pass a path inside the run"),
      ) as unknown as typeof fetch;

    render(<OpsArtifacts />);
    await user.click(await screen.findByTestId("ops-artifact-run-314"));
    await user.click(await screen.findByTestId("ops-artifact-file-314-0"));

    const error = await screen.findByTestId("ops-artifact-content-error");
    expect(error).toHaveTextContent("E_VALIDATION");
    expect(error).toHaveTextContent(/path traversal is refused/);
    expect(screen.queryByTestId("ops-artifact-content")).toBeNull();
  });

  it("Download fetches the byte route and announces what was saved", async () => {
    const user = userEvent.setup();
    mockGet.mockImplementation((path: string) => {
      if (path === "/artifacts") return Promise.resolve(INDEX);
      if (path === "/artifacts/314") return Promise.resolve(LISTING_314);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    const blob = new Blob(["# Brand"], { type: "application/octet-stream" });
    global.fetch = jest.fn((url: string) => {
      if (url.includes("/download?")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          blob: async () => blob,
          headers: new Headers({ "X-Polsia-Artifact-Bytes": "7" }),
        } as unknown as Response);
      }
      return Promise.resolve(contentResponse("# Brand", "abc123", 7));
    }) as unknown as typeof fetch;

    render(<OpsArtifacts />);
    await user.click(await screen.findByTestId("ops-artifact-run-314"));
    await user.click(await screen.findByTestId("ops-artifact-file-314-0"));
    await screen.findByTestId("ops-artifact-content");

    await user.click(screen.getByTestId("ops-artifact-download"));

    expect(await screen.findByTestId("ops-artifact-download-notice")).toHaveTextContent(
      /saved 315-brand-one-pager.md/,
    );
    const downloadCall = (global.fetch as jest.Mock).mock.calls.find((call) =>
      String(call[0]).includes("/download?"),
    );
    expect(downloadCall[0]).toContain("/api/v1/artifacts/314/download?path=notes%2F315-brand-one-pager.md");
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
  });
});
