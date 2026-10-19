/**
 * WAVE V21 / seat L4 — the app's missing 404 surface.
 *
 * Written BEFORE `frontend/src/app/not-found.tsx` exists, so the RED leg is a module-resolution
 * failure for `@/app/not-found` (raw/01-red.txt) — not a missing assertion.
 *
 * What is pinned here, and why:
 *   - the surface is a page SEGMENT, not a document: the app router nests a `not-found.tsx`
 *     inside `app/layout.tsx`, so it must never emit its own <html>/<body> and drop the shell;
 *   - the root carries `data-testid="not-found"` and the way home is `not-found-home` → /dashboard;
 *   - the menu's most-used work surfaces (Tasks, Agents, Social) are one click away, because a 404
 *     that only says "not found" makes the owner retype the address;
 *   - it does NOT echo the requested path. Next does not hand a path to this surface, and a
 *     client hook (`usePathname`) would only work after hydration while a handwritten path would
 *     be fiction — the source scan below forbids both routes to a fake echo;
 *   - rendering it produces NO console output (a 404 is a normal answer, not an error to log).
 */
import { readFileSync } from "fs";
import path from "path";

import { render, screen, within } from "@testing-library/react";

import NotFound from "@/app/not-found";

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink({
    href,
    children,
    ...rest
  }: { href: string; children: React.ReactNode } & Record<string, unknown>) {
    return (
      <a href={href} {...(rest as Record<string, string>)}>
        {children}
      </a>
    );
  },
}));

/** Read the product source itself: some honesty laws are structural, not renderable. */
const source = () =>
  readFileSync(path.resolve(__dirname, "..", "app", "not-found.tsx"), "utf8");

describe("app/not-found.tsx (V21 L4)", () => {
  it("renders as a page segment with one honest heading — never its own document shell", () => {
    const { container } = render(<NotFound />);

    const root = screen.getByTestId("not-found");
    expect(root).toBeInTheDocument();

    const heading = within(root).getByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent("This page does not exist");

    // A label states the HTTP fact plainly; it is not dressed up as a success or an error.
    expect(root).toHaveTextContent(/404/);

    // The shell belongs to app/layout.tsx. A second <html>/<body> here would replace it.
    expect(container.querySelector("html")).toBeNull();
    expect(container.querySelector("body")).toBeNull();
  });

  it("gives a way home (/dashboard) and the menu's most-used surfaces, all as real links", () => {
    render(<NotFound />);

    const home = screen.getByTestId("not-found-home");
    expect(home.tagName).toBe("A");
    expect(home).toHaveAttribute("href", "/dashboard");
    expect(home).toHaveTextContent(/dashboard/i);

    const destinations = screen.getByRole("navigation", { name: /common surfaces/i });
    for (const [label, href] of [
      [/tasks/i, "/tasks"],
      [/agents/i, "/agents"],
      [/social/i, "/social"],
    ] as const) {
      const link = within(destinations).getByRole("link", { name: label });
      expect(link).toHaveAttribute("href", href);
    }

    // Every destination is internal — a 404 must not send the owner off-site.
    for (const link of screen.getAllByRole("link")) {
      expect(link.getAttribute("href")).toMatch(/^\//);
    }
  });

  it("never echoes a path it was never given — no path-reading hook, no fabricated URL", () => {
    const productSource = source();

    // The two ways a path could appear: read it (client-only, and not supplied to this file by
    // Next) or write one down. Neither is allowed.
    for (const forbidden of [
      "usePathname",
      "useRouter",
      "useSearchParams",
      "searchParams",
      "headers()",
      "data-testid=\"not-found-path\"",
    ]) {
      expect(productSource).not.toContain(forbidden);
    }

    render(<NotFound />);
    const root = screen.getByTestId("not-found");
    expect(within(root).queryByTestId("not-found-path")).toBeNull();
    // No element in the surface claims to be the requested address.
    expect(root.querySelector("[data-path], [data-url]")).toBeNull();
  });

  it("renders without console output — a 404 is a normal answer, not a logged failure", () => {
    const spies = [
      jest.spyOn(console, "error").mockImplementation(() => {}),
      jest.spyOn(console, "warn").mockImplementation(() => {}),
      jest.spyOn(console, "log").mockImplementation(() => {}),
      jest.spyOn(console, "info").mockImplementation(() => {}),
    ];
    try {
      render(<NotFound />);
      expect(screen.getByTestId("not-found")).toBeInTheDocument();
      for (const spy of spies) expect(spy).not.toHaveBeenCalled();
    } finally {
      for (const spy of spies) spy.mockRestore();
    }
  });

  it("is written as a server segment (no client directive) so the shell can wrap it", () => {
    const productSource = source();
    expect(productSource.trimStart().startsWith('"use client"')).toBe(false);
    expect(productSource).not.toMatch(/<html[\s>]/i);
    expect(productSource).not.toMatch(/<body[\s>]/i);
  });
});
