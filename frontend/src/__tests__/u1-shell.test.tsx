import { readFileSync } from "fs";
import path from "path";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MenuNav } from "@/components/layout/MenuNav";
import { Topbar } from "@/components/layout/Topbar";
import { NAV_ITEMS, isActiveRoute } from "@/components/layout/nav-items";
import { Button, Card, Chip, SectionHeader } from "@/components/ui";

/**
 * U1 shell + design tokens.
 *
 * Scope: the app shell (topbar cluster, MENU nav dropdown, retired sidebar), the
 * shared UI primitives, and the token table's contrast floor. Sibling lanes own
 * the dashboard/feed suites; nothing here renders those.
 */

/** The path the mocked router reports; tests mutate it before rendering. */
let mockPathname = "/dashboard";

jest.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
}));

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink(props: { href: string; children: React.ReactNode } & Record<string, unknown>) {
    const { href, children, ...rest } = props;
    return (
      <a href={href} {...(rest as Record<string, string>)}>
        {children}
      </a>
    );
  },
}));

// Wave U2: the topbar bell now READS `GET /api/v1/notifications` (the endpoint this wave
// landed), so this suite mocks the read; the default answer is a true empty 200. The bell's
// count/error/loading/unread behaviours are pinned in coder F2's `u2-social-outreach.test.tsx`.
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(() => Promise.resolve({ items: [], count: 0 })) } };
});

const FRONTEND_ROOT = path.resolve(__dirname, "..");
const GLOBALS_CSS = readFileSync(path.join(FRONTEND_ROOT, "app", "globals.css"), "utf8");
const LAYOUT_TSX = readFileSync(path.join(FRONTEND_ROOT, "app", "layout.tsx"), "utf8");

const MENU_ROUTES = [
  "/dashboard",
  "/agents",
  "/tasks",
  // V9/B5 moved this pin 10 -> 11 in the SAME commit that added the Planner nav item; V11 unit 2
  // moved it 11 -> 14 in the SAME commit that added Invoices, Schedules and Support (owner-requested
  // operator surfaces). The dropdown is generated from NAV_ITEMS, so the count here is a claim
  // about the real table.
  "/planner",
  "/schedules",
  "/social",
  "/outreach",
  "/support",
  // V16/W3 moved this pin 14 -> 15 in the SAME commit that added the Phone route (the owner's
  // phone-link surface sits with the comms cluster). The dropdown is generated from NAV_ITEMS,
  // so the count here is a claim about the real table.
  "/phone",
  "/ads",
  "/finance",
  "/invoices",
  "/usage",
  "/memory",
  // V17/L4 moved this pin 15 -> 16 in the SAME edit that added the Capabilities route (the
  // read-only capability map joins the system cluster; Settings stays last). The dropdown is
  // generated from NAV_ITEMS, so the count here is a claim about the real table.
  "/capabilities",
  "/settings",
] as const;

function openMenu() {
  return screen.getByTestId("nav-menu-trigger");
}

beforeEach(() => {
  mockPathname = "/dashboard";
});

describe("topbar control cluster", () => {
  it("renders the bell, EDIT and MENU controls", () => {
    render(<Topbar />);
    expect(screen.getByTestId("topbar-notifications")).toHaveAccessibleName("Notifications");
    expect(screen.getByTestId("topbar-edit")).toHaveTextContent("Edit");
    expect(screen.getByTestId("nav-menu-trigger")).toHaveTextContent("Menu");
  });

  it("reports only counts that were actually measured (wave U2: the notifications endpoint exists)", async () => {
    const user = userEvent.setup();
    render(<Topbar />);
    const bell = screen.getByTestId("topbar-notifications");

    // The U1 rule held; what changed in U2 is WHERE a count may come from. The bell now reads
    // GET /api/v1/notifications (this suite's mock answers a true empty 200): an empty read
    // renders NO badge and no digit — a number here would still be invented.
    expect(bell.textContent ?? "").not.toMatch(/\d/);
    expect(bell).toHaveAttribute("aria-expanded", "false");

    await user.click(bell);

    expect(bell).toHaveAttribute("aria-expanded", "true");
    // OLD: the idle copy "no notifications endpoint" (removed with the endpoint).
    // NEW: the honest empty state of the real read.
    expect(await screen.findByTestId("notifications-empty")).toHaveTextContent(
      /No notifications in the last 48 hours/,
    );
  });

  it("EDIT opens the real Edit Dashboard sheet (V12-G1)", async () => {
    const user = userEvent.setup();
    render(<Topbar />);
    await user.click(screen.getByTestId("topbar-edit"));
    const sheet = screen.getByTestId("dashboard-editor");
    expect(sheet).toBeInTheDocument();
    // The sheet states its persistence truth in its own copy (browser-local).
    expect(screen.getByText(/saved in this browser only/i)).toBeInTheDocument();
  });

  it("keeps the app title in the shell", () => {
    render(<Topbar />);
    expect(screen.getByRole("link", { name: /dashboard/i })).toHaveTextContent("Polsia");
  });
});

describe("MENU nav dropdown", () => {
  it("is collapsed until pressed, then carries all sixteen routes", async () => {
    const user = userEvent.setup();
    render(<Topbar />);

    const trigger = openMenu();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();

    await user.click(trigger);

    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const links = within(nav).getAllByRole("link");
    expect(links).toHaveLength(16);
    expect(links.map((link) => link.getAttribute("href"))).toEqual([...MENU_ROUTES]);
    for (const item of NAV_ITEMS) {
      expect(within(nav).getByRole("link", { name: new RegExp(item.label, "i") })).toBeInTheDocument();
    }
  });

  it("marks the current page with aria-current AND a non-colour cue", async () => {
    mockPathname = "/tasks";
    const user = userEvent.setup();
    render(<Topbar />);
    await user.click(openMenu());

    const nav = screen.getByRole("navigation", { name: "Main" });
    const active = within(nav).getByRole("link", { name: /tasks/i });
    const inactive = within(nav).getByRole("link", { name: /finance/i });

    // Announced state (audit Q-15) …
    expect(active).toHaveAttribute("aria-current", "page");
    expect(inactive).not.toHaveAttribute("aria-current");
    // … and cues that survive a colour-blind or low-contrast reading: a marker
    // glyph plus bold weight, not just a different fill.
    expect(active.textContent).toContain("▸");
    expect(inactive.textContent).not.toContain("▸");
    expect(active).toHaveClass("font-bold");
    expect(inactive).not.toHaveClass("font-bold");
    expect(within(nav).getAllByRole("link", { current: "page" })).toHaveLength(1);
  });

  it("closes on Escape and hands focus back to the trigger", async () => {
    const user = userEvent.setup();
    render(<Topbar />);
    const trigger = openMenu();
    await user.click(trigger);
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("closes on a press outside, and toggles closed on a second press of MENU", async () => {
    const user = userEvent.setup();
    render(<Topbar />);

    await user.click(openMenu());
    await user.click(document.body);
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();

    await user.click(openMenu());
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    await user.click(openMenu());
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
  });

  it("keeps focus inside the panel while tabbing through it, and closes once focus leaves", async () => {
    const user = userEvent.setup();
    render(<Topbar />);
    await user.click(openMenu());

    await user.tab(); // trigger → first menu item
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(nav).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /dashboard/i })).toHaveFocus();

    // Sixteen items, so sixteen tabs leave the panel (the pin moved with the nav table).
    for (let i = 0; i < 16; i += 1) {
      await user.tab();
    }
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
  });

  it("closes after choosing a route", async () => {
    const user = userEvent.setup();
    render(<Topbar />);
    await user.click(openMenu());

    await user.click(screen.getByRole("link", { name: /finance/i }));

    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
  });
});

describe("isActiveRoute", () => {
  it("matches on segment boundaries, so /tasks does not claim /tasks-archive", () => {
    expect(isActiveRoute("/tasks", "/tasks")).toBe(true);
    expect(isActiveRoute("/tasks/123", "/tasks")).toBe(true);
    expect(isActiveRoute("/tasks-archive", "/tasks")).toBe(false);
    expect(isActiveRoute("/dashboard", "/tasks")).toBe(false);
    expect(isActiveRoute(null, "/tasks")).toBe(false);
    expect(isActiveRoute(undefined, "/tasks")).toBe(false);
  });
});

describe("app shell", () => {
  it("retires the persistent sidebar and wires the topbar instead", () => {
    expect(LAYOUT_TSX).toContain("Topbar");
    expect(LAYOUT_TSX).not.toMatch(/Sidebar/);
    expect(LAYOUT_TSX).toContain('href="#content"');
    expect(LAYOUT_TSX).toContain('id="content"');
    expect(() => readFileSync(path.join(FRONTEND_ROOT, "components", "layout", "Sidebar.tsx"), "utf8")).toThrow();
  });

  it("keeps the whole navigation reachable without a sidebar", async () => {
    const user = userEvent.setup();
    // The shell renders NO nav outside the dropdown: one <nav>, and only when MENU is open.
    render(<Topbar />);
    expect(screen.queryAllByRole("navigation")).toHaveLength(0);
    await user.click(openMenu());
    expect(screen.queryAllByRole("navigation")).toHaveLength(1);
  });
});

describe("ui primitives", () => {
  it("Button defaults to the beveled recipe and offers the black glossy CTA", () => {
    const { rerender } = render(<Button>See plans</Button>);
    const bevel = screen.getByRole("button", { name: "See plans" });
    expect(bevel).toHaveClass("btn", "btn-bevel");
    expect(bevel).toHaveAttribute("type", "button");
    expect(bevel).toHaveAttribute("data-variant", "bevel");

    rerender(
      <Button variant="glossy" size="sm" disabled>
        See plans
      </Button>,
    );
    const glossy = screen.getByRole("button", { name: "See plans" });
    expect(glossy).toHaveClass("btn-glossy");
    expect(glossy).not.toHaveClass("btn-bevel");
    expect(glossy).toBeDisabled();
  });

  it("Chip renders the caps recipe with its variant fill", () => {
    render(
      <>
        <Chip>2D AGO</Chip>
        <Chip variant="dark">Feature</Chip>
        <Chip variant="accent">Auto mode</Chip>
        <Chip variant="alert">New</Chip>
      </>,
    );
    expect(screen.getByText("2D AGO")).toHaveClass("chip", "chip-soft");
    expect(screen.getByText("Feature")).toHaveClass("chip-dark");
    expect(screen.getByText("Auto mode")).toHaveClass("chip-accent");
    expect(screen.getByText("New")).toHaveClass("chip-alert");
  });

  it("SectionHeader and Card render a heading row with an action slot", () => {
    render(
      <Card kicker="Tasks" title="Today" action={<Button>See all tasks</Button>}>
        <SectionHeader title="Documents" action={<Button>View all (6)</Button>} />
      </Card>,
    );
    expect(screen.getByRole("heading", { name: "Today" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Documents" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "See all tasks" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View all (6)" })).toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
 * Token table (aid3 B18 acceptance): every text token ≥ 4.5:1 on each surface it
 * is used on. The ratios are computed from `globals.css` itself, so a token edit
 * cannot slip under the floor unnoticed.
 * ------------------------------------------------------------------------- */

type Hsl = { h: number; s: number; l: number };

function parseTokenTable(css: string): Map<string, Hsl> {
  const tokens = new Map<string, Hsl>();
  const pattern = /--([a-z0-9-]+):\s*([0-9.]+)\s+([0-9.]+)%\s+([0-9.]+)%\s*;/g;
  let match = pattern.exec(css);
  while (match !== null) {
    tokens.set(match[1], { h: Number(match[2]), s: Number(match[3]), l: Number(match[4]) });
    match = pattern.exec(css);
  }
  return tokens;
}

function channelToLinear(value: number): number {
  return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance({ h, s, l }: Hsl): number {
  const saturation = s / 100;
  const lightness = l / 100;
  const chroma = (1 - Math.abs(2 * lightness - 1)) * saturation;
  const hueSegment = h / 60;
  const x = chroma * (1 - Math.abs((hueSegment % 2) - 1));
  const [r1, g1, b1] =
    hueSegment < 1
      ? [chroma, x, 0]
      : hueSegment < 2
        ? [x, chroma, 0]
        : hueSegment < 3
          ? [0, chroma, x]
          : hueSegment < 4
            ? [0, x, chroma]
            : hueSegment < 5
              ? [x, 0, chroma]
              : [chroma, 0, x];
  const offset = lightness - chroma / 2;
  return (
    0.2126 * channelToLinear(r1 + offset) +
    0.7152 * channelToLinear(g1 + offset) +
    0.0722 * channelToLinear(b1 + offset)
  );
}

function contrastRatio(a: Hsl, b: Hsl): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const lighter = Math.max(la, lb);
  const darker = Math.min(la, lb);
  return (lighter + 0.05) / (darker + 0.05);
}

/** [label, foreground token, background token] — every pair the shell/primitive CSS uses. */
const TEXT_USAGE: ReadonlyArray<readonly [string, string, string]> = [
  ["body text / canvas", "ink", "canvas"],
  ["body text / card", "ink", "surface"],
  ["meta text / canvas", "ink-muted", "canvas"],
  ["meta text / card", "ink-muted", "surface"],
  ["age chip / chip fill", "ink-muted", "surface-strong"],
  ["band text / band", "band-ink", "band"],
  ["dark chip / band fill", "band-ink", "band"],
  ["link / card", "link", "surface"],
  ["link / canvas", "link", "canvas"],
  ["alert chip", "alert-ink", "alert"],
  ["accent chip", "accent-ink", "accent"],
];

describe("design tokens", () => {
  const tokens = parseTokenTable(GLOBALS_CSS);

  it("defines the roles the shell and primitives consume", () => {
    for (const token of [
      "canvas",
      "surface",
      "surface-sunk",
      "surface-strong",
      "band",
      "band-ink",
      "ink",
      "ink-muted",
      "line",
      "line-strong",
      "link",
      "accent",
      "accent-soft",
      "accent-ink",
      "alert",
      "alert-ink",
      "focus",
    ]) {
      expect(tokens.has(token)).toBe(true);
    }
  });

  it("keeps every text-on-surface pair at WCAG AA or better", () => {
    const failures: string[] = [];
    const measured: string[] = [];
    for (const [label, foreground, background] of TEXT_USAGE) {
      const fg = tokens.get(foreground);
      const bg = tokens.get(background);
      expect(fg).toBeDefined();
      expect(bg).toBeDefined();
      const ratio = contrastRatio(fg as Hsl, bg as Hsl);
      measured.push(`${label} (${foreground} on ${background}): ${ratio.toFixed(2)}:1`);
      if (ratio < 4.5) {
        failures.push(`${label} (${foreground} on ${background}): ${ratio.toFixed(2)}:1 < 4.5:1`);
      }
    }
    expect(failures).toEqual([]);
    // Belt and braces: the table is real, not empty.
    expect(measured).toHaveLength(TEXT_USAGE.length);
  });

  it("uses system font stacks only — no webfont URLs, no lifted assets", () => {
    expect(GLOBALS_CSS).toMatch(/--font-serif:/);
    expect(GLOBALS_CSS).toMatch(/--font-mono:/);
    expect(GLOBALS_CSS).not.toMatch(/@font-face|fonts\.googleapis|\.woff|url\(/);
  });

  it("draws focus as an outline and honours prefers-reduced-motion (Q-28)", () => {
    expect(GLOBALS_CSS).toMatch(/:focus-visible\s*\{[^}]*outline:\s*2px solid hsl\(var\(--focus\)\)/);
    expect(GLOBALS_CSS).toMatch(/prefers-reduced-motion:\s*reduce/);
  });
});

describe("MenuNav standalone", () => {
  it("renders the same sixteen routes as the shared nav table", async () => {
    const user = userEvent.setup();
    render(<MenuNav />);
    await user.click(screen.getByTestId("nav-menu-trigger"));
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getAllByRole("link")).toHaveLength(NAV_ITEMS.length);
  });
});
