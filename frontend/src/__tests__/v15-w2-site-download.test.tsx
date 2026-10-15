/**
 * WAVE V15 / seat W2 — item 4-07 (S3-01): the site-package section must ship a REAL download
 * control and stop saying the false thing.
 *
 * Wire truth (read from the backend before writing this):
 *   - GET /api/v1/site/package/download answers the built package as a ZIP with
 *     `Content-Disposition: attachment; filename="site-<slug>.zip"` (`site.py:29-45`);
 *   - an unsafe slug is the named `E_VALIDATION`, an unbuilt one the named `E_NOT_FOUND`
 *     (`site_package_service.build_package_archive`) — refusals arrive in the `{detail:{…}}`
 *     wire shape;
 *   - the api client is JSON-only by construction, so the download is a raw `fetch` + blob, the
 *     exact pattern `components/usage/ExportButton.tsx:64-90` established (auth from the
 *     client's single source of truth, `API_KEY`).
 *
 * RED leg (raw in wave-v15/w2/raw/): pre-fix there is NO download control at all and the copy
 * claims "there is no HTTP download for this package" (`SitePackageSection.tsx:122-123`,
 * false since the route shipped) — every pin here fails before the fix.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { post: jest.fn() },
    // The one credential source: a NON-empty key here proves the header actually rides the wire.
    API_KEY: "test-key",
  };
});

import { SitePackageSection } from "@/components/settings/SitePackageSection";

const FETCH_MOCK = jest.fn();

let anchorClick: jest.SpyInstance;
let clicked: Array<{ download: string; href: string }> = [];

beforeEach(() => {
  FETCH_MOCK.mockReset();
  global.fetch = FETCH_MOCK as unknown as typeof fetch;
  clicked = [];
  anchorClick = jest
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ download: this.download, href: this.href });
    });
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: jest.fn(() => "blob:site-package"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    writable: true,
    value: jest.fn(),
  });
});

afterEach(() => {
  anchorClick.mockRestore();
});

const REPORTED = {
  package_dir: "C:\\Polsia\\backend\\data\\exports\\site-polsia",
  files: [
    "C:\\Polsia\\backend\\data\\exports\\site-polsia\\index.html",
    "C:\\Polsia\\backend\\data\\exports\\site-polsia\\HOSTING-README.md",
  ],
  slug: "polsia",
};

describe("4-07 — site-package download", () => {
  it("the control exists and downloads the REAL zip response as a blob, named by Content-Disposition", async () => {
    FETCH_MOCK.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: {
        get: (name: string) => {
          if (name.toLowerCase() === "content-disposition") {
            return 'attachment; filename="site-polsia.zip"';
          }
          return null;
        },
      },
      blob: () => Promise.resolve(new Blob(["zip-bytes"], { type: "application/zip" })),
      text: () => Promise.resolve(""),
    });

    render(<SitePackageSection />);
    fireEvent.click(screen.getByTestId("settings-site-download"));

    await waitFor(() => expect(FETCH_MOCK).toHaveBeenCalledTimes(1));
    const [url, init] = FETCH_MOCK.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/api\/v1\/site\/package\/download$/);
    // The api client's single credential source, sent exactly when it is non-empty.
    expect((init.headers as Record<string, string>)["X-API-Key"]).toBe("test-key");

    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    // The save name is the SERVER's, parsed out of the header — never invented client-side.
    expect(clicked[0].download).toBe("site-polsia.zip");
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("settings-site-download-done")).toHaveTextContent(
      "Downloaded site-polsia.zip.",
    );
  });

  it("a header-less Content-Disposition still saves the real bytes under a STATED fallback name", async () => {
    FETCH_MOCK.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: { get: () => null },
      blob: () => Promise.resolve(new Blob(["zip-bytes"], { type: "application/zip" })),
      text: () => Promise.resolve(""),
    });

    render(<SitePackageSection />);
    fireEvent.click(screen.getByTestId("settings-site-download"));

    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(clicked[0].download).toBe("site-package.zip");
    expect(await screen.findByTestId("settings-site-download-done")).toHaveTextContent(
      "Downloaded site-package.zip.",
    );
  });

  it("parses the UNQUOTED filename form too", async () => {
    FETCH_MOCK.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: {
        get: (name: string) =>
          name.toLowerCase() === "content-disposition" ? "attachment; filename=site-polsia.zip" : null,
      },
      blob: () => Promise.resolve(new Blob(["zip-bytes"], { type: "application/zip" })),
      text: () => Promise.resolve(""),
    });

    render(<SitePackageSection />);
    fireEvent.click(screen.getByTestId("settings-site-download"));

    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(clicked[0].download).toBe("site-polsia.zip");
  });

  it("an unbuilt/unavailable package renders the server's named refusal — no blob, no anchor, no dead button", async () => {
    FETCH_MOCK.mockResolvedValue({
      ok: false,
      status: 404,
      statusText: "Not Found",
      headers: { get: () => null },
      text: () =>
        Promise.resolve(
          JSON.stringify({
            detail: {
              code: "E_NOT_FOUND",
              message: "no site package has been built for this company",
              how_to_fix: "build it first (POST /api/v1/site/package)",
            },
          }),
        ),
      blob: () => Promise.resolve(new Blob([])),
    });

    render(<SitePackageSection />);
    fireEvent.click(screen.getByTestId("settings-site-download"));

    const alert = await screen.findByTestId("settings-site-download-error");
    expect(alert).toHaveTextContent("E_NOT_FOUND");
    expect(alert).toHaveTextContent("no site package has been built");
    expect(alert).toHaveTextContent("build it first");
    expect(anchorClick).not.toHaveBeenCalled();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("a transport failure is named too — never a silent no-op", async () => {
    FETCH_MOCK.mockRejectedValue(new TypeError("Failed to fetch"));

    render(<SitePackageSection />);
    fireEvent.click(screen.getByTestId("settings-site-download"));

    const alert = await screen.findByTestId("settings-site-download-error");
    expect(alert).toHaveTextContent("Failed to fetch");
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("the copy states the truth: the ZIP download exists and nothing is published by it", async () => {
    render(<SitePackageSection />);
    // The control is present before any build — the server may already hold a package.
    expect(screen.getByTestId("settings-site-download")).toBeInTheDocument();
    // The false sentence must be gone from the component's own source and from the DOM.
    expect(screen.queryByText(/no HTTP download/)).toBeNull();
    expect(
      screen.getByText(/Download it as a ZIP \(GET \/api\/v1\/site\/package\/download\)/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/nothing is published by building it/)).toBeNull();
  });

  it("still reports the REAL build response — paths, slug, no fabricated progress", async () => {
    const { api } = jest.requireMock("@/lib/api") as { api: { post: jest.Mock } };
    api.post.mockResolvedValueOnce(REPORTED);

    render(<SitePackageSection />);
    fireEvent.click(screen.getByTestId("settings-site-build"));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/site/package"));
    const result = await screen.findByTestId("settings-site-result");
    expect(result).toHaveTextContent("polsia");
    expect(result).toHaveTextContent("index.html");
    expect(screen.queryByRole("progressbar")).toBeNull();
  });
});
