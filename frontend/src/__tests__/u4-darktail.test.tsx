import { fireEvent, render, screen } from "@testing-library/react";
import fs from "node:fs";
import path from "node:path";

import GlobalError from "@/app/global-error";
import SegmentError from "@/app/error";

/**
 * Wave U4, seat B — the dark-DOM tail (app/error.tsx restyle + dead-set labels).
 *
 * RED leg: authored before the restyle. Against the old tree this file fails on
 * (a) the old-dark classes still present in `app/error.tsx` / dark hexes in `app/global-error.tsx`,
 * (b) the missing why-comments in the proven-dead pair, (c) the stale hook comment. After the U4
 * restyle it passes and pins:
 *  - the error surfaces render on the U1 design language (ui Button primitive; zero old-dark
 *    classes in source AND in the rendered subtree), and
 *  - the behavior contract is unchanged (reset wiring, message / digest / request-id conditions).
 */

const ROOT = process.cwd();

function readSource(relativePath: string): string {
  return fs.readFileSync(path.join(ROOT, relativePath), "utf8");
}

/** The pre-U1 dark vocabulary this wave removes. `indigo` is matched as a substring: every
 *  indigo utility (`bg-indigo-600`, `hover:bg-indigo-700`) belonged to the same old button. */
const OLD_DARK_TOKENS = [
  "text-white",
  "bg-gray-800",
  "bg-gray-900",
  "bg-gray-700",
  "border-gray-700",
  "text-gray-300",
  "text-gray-400",
  "text-gray-500",
  "indigo",
];

/** Dark hex values the old global-error surface inlined. */
const OLD_DARK_HEXES = ["#020617", "#0f172a", "#1e293b", "#4f46e5", "#334155"];

/** The same values plus the rgb() forms jsdom may serialize a style attribute into, so the
 *  rendered-DOM half catches both spellings. */
const OLD_DARK_RENDERED = [
  ...OLD_DARK_HEXES,
  "rgb(2, 6, 23)",
  "rgb(15, 23, 42)",
  "rgb(30, 41, 59)",
  "rgb(79, 70, 229)",
  "rgb(51, 65, 85)",
];

/** All class attributes of the rendered subtree, joined — the rendered-DOM half of the check. */
function renderedClassText(container: HTMLElement): string {
  return Array.from(container.querySelectorAll("*"))
    .map((el) => el.getAttribute("class") ?? "")
    .join(" ");
}

/** All inline style attributes of the rendered subtree, joined. */
function renderedStyleText(container: HTMLElement): string {
  return Array.from(container.querySelectorAll("*"))
    .map((el) => el.getAttribute("style") ?? "")
    .join(" ");
}

const WHY_COMMENT =
  "Unwired since the U1 dashboard rebuild — zero non-test imports as of wave U4; kept per the campaign no-delete rule; wire-vs-retire decision recorded in REQUESTS.";

/** Whitespace-insensitive matching so an autoformatter re-wrapping the comment cannot
 *  silently un-test it. */
function normalized(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

describe("u4-darktail — app/error.tsx (U1 design language, behavior identical)", () => {
  it("carries zero old-dark classes in source and in the rendered subtree", () => {
    const source = readSource("src/app/error.tsx");
    for (const token of OLD_DARK_TOKENS) {
      expect(source).not.toContain(token);
    }

    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const error = Object.assign(new Error("boom"), { digest: "digest-1", requestId: "req-1" });
    const { container } = render(<SegmentError error={error} reset={jest.fn()} />);

    const rendered = renderedClassText(container);
    for (const token of OLD_DARK_TOKENS) {
      expect(rendered).not.toContain(token);
    }
    consoleError.mockRestore();
  });

  it("uses the ui Button primitive: glossy primary + bevel secondary", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const { container } = render(<SegmentError error={new Error("boom")} reset={jest.fn()} />);

    expect(container.querySelectorAll("button.btn").length).toBe(2);
    const glossy = container.querySelector('button[data-variant="glossy"]');
    const bevel = container.querySelector('button[data-variant="bevel"]');
    expect(glossy).not.toBeNull();
    expect(bevel).not.toBeNull();
    expect(glossy?.textContent).toBe("Try again");
    expect(bevel?.textContent).toBe("Reload the page");
    consoleError.mockRestore();
  });

  it("keeps its reset wiring — Try again calls reset(); the reload escape stays a document reload", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const reset = jest.fn();
    render(<SegmentError error={new Error("boom")} reset={reset} />);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(reset).toHaveBeenCalledTimes(1);
    // jsdom cannot navigate, so the reload escape hatch is pinned on the source, not by clicking.
    expect(readSource("src/app/error.tsx")).toContain("window.location.reload()");
    consoleError.mockRestore();
  });

  it("keeps the error-detail conditions: message fallback, digest only when present, request-id only when set", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);

    const { unmount } = render(<SegmentError error={new Error("")} reset={jest.fn()} />);
    expect(screen.getByTestId("error-message")).toHaveTextContent(
      "Unknown error (no message was attached)",
    );
    expect(screen.queryByText(/^Digest:/)).toBeNull();
    expect(screen.queryByTestId("request-id")).toBeNull();
    unmount();

    const withMeta = Object.assign(new Error("boom"), { digest: "d-9" });
    render(<SegmentError error={withMeta} reset={jest.fn()} />);
    expect(screen.getByText(/^Digest: d-9$/)).toBeInTheDocument();
    consoleError.mockRestore();
  });
});

describe("u4-darktail — the proven-dead pair is labelled (why-comments on disk)", () => {
  it("MetricsCard.tsx carries the wave-U4 why-comment at the top", () => {
    const source = normalized(readSource("src/components/dashboard/MetricsCard.tsx"));
    const at = source.indexOf(normalized(WHY_COMMENT));
    expect(at).toBeGreaterThan(-1);
    expect(at).toBeLessThan(400);
  });

  it("AgentStatusGrid.tsx carries the wave-U4 why-comment at the top", () => {
    const source = normalized(readSource("src/components/dashboard/AgentStatusGrid.tsx"));
    const at = source.indexOf(normalized(WHY_COMMENT));
    expect(at).toBeGreaterThan(-1);
    expect(at).toBeLessThan(400);
  });
});

describe("u4-darktail — useAgentStatus stays alive and its comment tells the truth", () => {
  it("drops the stale 'exactly one consumer' claim and names the real live consumers", () => {
    const source = readSource("src/hooks/useAgentStatus.ts");
    expect(source).not.toContain("exactly one consumer");
    expect(source).toContain("AgentsRoster");
    expect(source).toContain("PolsiaStatusCard");
  });
});

describe("u4-darktail — app/global-error.tsx (inline light restyle, still self-contained)", () => {
  it("no dark hex values remain in source or in the rendered inline styles", () => {
    const source = readSource("src/app/global-error.tsx").toLowerCase();
    for (const hex of OLD_DARK_HEXES) {
      expect(source).not.toContain(hex);
    }

    const { container } = render(<GlobalError error={new Error("root exploded")} reset={jest.fn()} />);
    const styles = renderedStyleText(container).toLowerCase();
    for (const dark of OLD_DARK_RENDERED) {
      expect(styles).not.toContain(dark);
    }
  });

  it("stays self-contained: the body carries a light inline background and reset still fires", () => {
    const reset = jest.fn();
    const { container } = render(<GlobalError error={new Error("root exploded")} reset={reset} />);

    const bodyStyle = container.querySelector("body")?.getAttribute("style") ?? "";
    expect(bodyStyle).toContain("background");
    const bodyLc = bodyStyle.toLowerCase();
    expect(bodyLc.includes("#f0f0f0") || bodyLc.includes("rgb(240, 240, 240)")).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(reset).toHaveBeenCalledTimes(1);
  });
});

describe("u4-darktail — hostile inputs (P4)", () => {
  it("an error with a NULL digest renders without a Digest line", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const error = Object.assign(new Error("boom"), { digest: null }) as unknown as Error & {
      digest?: string;
    };

    render(<SegmentError error={error} reset={jest.fn()} />);

    expect(screen.getByTestId("error-message")).toHaveTextContent("boom");
    expect(screen.queryByText(/^Digest:/)).toBeNull();
    consoleError.mockRestore();
  });

  it("an empty-string digest renders no Digest line either (same truthiness guard as before)", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const error = Object.assign(new Error("boom"), { digest: "" });

    render(<SegmentError error={error} reset={jest.fn()} />);

    expect(screen.queryByText(/^Digest:/)).toBeNull();
    consoleError.mockRestore();
  });

  it("a whitespace-only message is shown literally — the boundary never hides what it received", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);

    render(<SegmentError error={new Error("   ")} reset={jest.fn()} />);

    expect(screen.getByTestId("error-message").textContent).toBe("   ");
    consoleError.mockRestore();
  });

  it("a NON-STRING requestId is treated as absent (generic advice, no crash, no empty shell)", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const error = Object.assign(new Error("boom"), { requestId: 42 });

    render(<SegmentError error={error} reset={jest.fn()} />);

    expect(screen.queryByTestId("request-id")).toBeNull();
    expect(screen.getByText(/backend log for this request \(X-Request-ID\)/)).toBeInTheDocument();
    consoleError.mockRestore();
  });

  it("a padded requestId is trimmed before display", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const error = Object.assign(new Error("boom"), { requestId: "  req-77  " });

    render(<SegmentError error={error} reset={jest.fn()} />);

    expect(screen.getByTestId("request-id")).toHaveTextContent("req-77");
    consoleError.mockRestore();
  });
});
