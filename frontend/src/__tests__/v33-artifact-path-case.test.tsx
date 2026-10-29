/**
 * WAVE 5 / F-35 — the artifact path PRINTED on screen must be the path the API ACCEPTS.
 *
 * THE DEFECT (measured live on the wave-4 build): the path control carries the `.ui-label`
 * typography class, which sets `text-transform: uppercase`; so the card shows
 * `BRAND/BRAND-ONE-PAGER.MD` for a workspace file whose real, case-sensitive name is
 * `brand/brand-one-pager.md`. Copying the visible string into
 * `GET /artifacts/316/content?path=BRAND/BRAND-ONE-PAGER.MD` answers **404 E_NOT_FOUND** while the
 * lowercase path answers 200. The card's own open/download works because it passes the DOM value,
 * so the lie is confined to what a human reads and copies.
 *
 * WHAT THIS PINS:
 *   * the file control prints the path EXACTLY as the server listed it (`textContent === path`) and
 *     carries an inline `text-transform: none`, so no stylesheet can change the case of its text;
 *   * the open request carries the SAME path the control printed (case included);
 *   * the preview header names the selected path verbatim (a second place the owner copies from);
 *   * the download notice names the saved file verbatim — an uppercased filename is the same lie;
 *   * the same rules hold on BOTH surfaces that render paths (`/ops` and the shared run panel used
 *     by `/runs/{id}` and `/tasks/{id}`), because the style constant lives in the shared reader.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { ...actual.api, get: jest.fn(), post: jest.fn(), patch: jest.fn() } };
});

import { api } from "@/lib/api";
import { RunArtifacts } from "@/components/artifacts/RunArtifacts";
import { OpsArtifacts } from "@/components/ops/OpsArtifacts";

const mockGet = api.get as jest.Mock;

/** The real on-disk name of run 316's file — lowercase path segments, a `.md` extension. */
const REAL_PATH = "brand/brand-one-pager.md";
const SECOND_PATH = "brand/label-data-checklist.md";

const LISTING_316 = {
  run_id: "316",
  items: [
    { path: REAL_PATH, bytes: 5721, modified_at: "2026-10-28T21:25:40Z" },
    { path: SECOND_PATH, bytes: 11348, modified_at: "2026-10-28T21:25:41Z" },
  ],
  total: 2,
  total_bytes: 17069,
  truncated: false,
  skipped_links: 0,
};

const INDEX_316 = {
  items: [
    {
      run_id: "316",
      files: 2,
      bytes: 17069,
      latest_modified_at: "2026-10-28T21:25:40Z",
      truncated: false,
    },
  ],
  total: 1,
  truncated: false,
  skipped: [],
};

function contentResponse(text: string, sha256: string, bytes: number): Response {
  return {
    ok: true,
    status: 200,
    text: async () => text,
    headers: new Headers({
      "X-Meridian-Artifact-Sha256": sha256,
      "X-Meridian-Artifact-Bytes": String(bytes),
      "X-Meridian-Artifact-Modified": "2026-10-28T21:25:40Z",
    }),
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

/** The one CSS mechanism F-35 names: an element whose computed text-transform is `uppercase`. */
function expectVerbatimCase(node: HTMLElement, path: string): void {
  // The text a reader sees/copies is the element's textContent — never a stylesheet's re-casing.
  expect(node.textContent).toBe(path);
  // Inline `none` beats `.ui-label`'s uppercase regardless of stylesheet/layer order.
  expect(node).toHaveStyle({ textTransform: "none" });
}

describe("F-35 — /ops prints the servable path verbatim", () => {
  it("the file control, the preview header and the content request agree on the exact case", async () => {
    const user = userEvent.setup();
    mockGet.mockImplementation((path: string) => {
      if (path === "/artifacts") return Promise.resolve(INDEX_316);
      if (path === "/artifacts/316") return Promise.resolve(LISTING_316);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    global.fetch = jest
      .fn()
      .mockResolvedValue(contentResponse("# Brand one-pager", "abc123", 5721)) as unknown as typeof fetch;

    render(<OpsArtifacts />);
    await screen.findByTestId("ops-artifact-run-316");
    await user.click(screen.getByTestId("ops-artifact-run-316"));

    const fileButton = await screen.findByTestId("ops-artifact-file-316-0");
    expectVerbatimCase(fileButton, REAL_PATH);

    await user.click(fileButton);
    await screen.findByTestId("ops-artifact-content");

    // The preview header is the second place a reader copies the path from.
    const selectionPath = screen.getByTestId("ops-artifact-selection-path");
    expectVerbatimCase(selectionPath, REAL_PATH);

    // The request the click made carries the SAME string the control printed.
    const call = (global.fetch as jest.Mock).mock.calls[0];
    expect(call[0]).toContain(`/api/v1/artifacts/316/content?path=${encodeURIComponent(REAL_PATH)}`);
    expect(call[0]).not.toContain("BRAND");
  });

  it("the download notice names the saved file verbatim", async () => {
    const user = userEvent.setup();
    mockGet.mockImplementation((path: string) => {
      if (path === "/artifacts") return Promise.resolve(INDEX_316);
      if (path === "/artifacts/316") return Promise.resolve(LISTING_316);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    const blob = new Blob(["# Brand one-pager"], { type: "text/markdown" });
    global.fetch = jest.fn((url: string) => {
      if (String(url).includes("/download?")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          blob: async () => blob,
          headers: new Headers({ "X-Meridian-Artifact-Bytes": "5721" }),
        } as unknown as Response);
      }
      return Promise.resolve(contentResponse("# Brand one-pager", "abc123", 5721));
    }) as unknown as typeof fetch;

    render(<OpsArtifacts />);
    await screen.findByTestId("ops-artifact-run-316");
    await user.click(screen.getByTestId("ops-artifact-run-316"));
    await user.click(await screen.findByTestId("ops-artifact-file-316-0"));
    await screen.findByTestId("ops-artifact-content");
    await user.click(screen.getByTestId("ops-artifact-download"));

    const notice = await screen.findByTestId("ops-artifact-download-notice");
    // The notice sizes the SAVED blob (the test blob is 17 B), and names it in the server's case.
    expect(notice).toHaveTextContent("saved brand-one-pager.md (17 B)");
    expect(notice.textContent ?? "").not.toContain("BRAND-ONE-PAGER.MD");
    expect(notice).toHaveStyle({ textTransform: "none" });
  });
});

describe("F-35 — the shared run panel (/runs and /tasks) prints the servable path verbatim", () => {
  it("the file control and its preview header keep the exact server case", async () => {
    const user = userEvent.setup();
    mockGet.mockImplementation((path: string) => {
      if (path === "/artifacts/316") return Promise.resolve(LISTING_316);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    global.fetch = jest
      .fn()
      .mockResolvedValue(contentResponse("# Brand one-pager", "abc123", 5721)) as unknown as typeof fetch;

    render(<RunArtifacts runId={316} />);

    const card = await screen.findByTestId("run-artifacts");
    const fileButton = await within(card).findByTestId("run-artifact-file-0");
    expectVerbatimCase(fileButton, REAL_PATH);

    await user.click(fileButton);
    await screen.findByTestId("run-artifact-content");
    expectVerbatimCase(screen.getByTestId("run-artifact-selection-path"), REAL_PATH);

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const call = (global.fetch as jest.Mock).mock.calls[0];
    expect(call[0]).toContain(`/api/v1/artifacts/316/content?path=${encodeURIComponent(REAL_PATH)}`);
  });
});
