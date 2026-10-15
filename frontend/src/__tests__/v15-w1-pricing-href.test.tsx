import { fireEvent, render, screen } from "@testing-library/react";

/**
 * WAVE V15 / W1 — item 2R-09: PricingSection source-link hardening.
 *
 * The pricing catalog feed is static and NOT attacker-influenced (low severity), but the sink is
 * the U2-04 class: `settings/PricingSection.tsx` rendered `<a href={row.source_url}>` UNGUARDED.
 * The fix reuses the EXISTING exported `safeHref()` from `planner/CandidateCard.tsx`
 * (import-only — that file is not edited) with the same discipline: an anchor only for http(s),
 * plain text otherwise.
 *
 * These pins fix the DOM contract:
 *   - a `javascript:`/"data:"/relative/malformed source_url renders as TEXT, and no anchor in the
 *     section ever carries that URL;
 *   - a legit https row still renders an anchor with the exact href + target/rel;
 *   - the row's label discipline is unchanged (`source_label ?? source_url`, `—` when neither).
 */

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { api } from "@/lib/api";
import { PricingSection } from "@/components/settings/PricingSection";

const mockGet = api.get as jest.Mock;

afterEach(() => {
  mockGet.mockReset();
});

type PricingRow = {
  service: string;
  price_low: number | null;
  price_high: number | null;
  model: string | null;
  hours: { low: number | null; high: number | null } | null;
  source_url: string | null;
  fetch_type: string | null;
  date: string | null;
  source_label: string | null;
  taxable_hint: string | null;
};

function row(overrides: Partial<PricingRow>): PricingRow {
  return {
    service: "row",
    price_low: null,
    price_high: null,
    model: null,
    hours: null,
    source_url: null,
    fetch_type: null,
    date: "2026-10-14",
    source_label: null,
    taxable_hint: null,
    ...overrides,
  };
}

const CATALOG = {
  query: null,
  count: 4,
  items: [
    row({
      service: "Hostile no-label row",
      source_url: "javascript:alert(document.cookie)",
    }),
    row({
      service: "Legit https row",
      source_url: "https://arc4.com/resources/local-seo-pricing/",
      source_label: "https://arc4.com/resources/local-seo-pricing/ (F)",
    }),
    row({
      service: "Data-scheme row",
      source_url: "data:text/html,<script>alert(1)</script>",
      source_label: "looks legit",
    }),
    row({
      service: "Relative row",
      source_url: "/relative/path",
    }),
  ],
  catalog_as_of: "2026-10-14",
  staleness_days: 1,
  stale: false,
};

async function load() {
  mockGet.mockResolvedValue(CATALOG);
  const view = render(<PricingSection />);
  fireEvent.click(screen.getByTestId("settings-pricing-load"));
  await screen.findByTestId("settings-pricing-provenance");
  return view;
}

describe("2R-09 — pricing source href hardening (safeHref discipline)", () => {
  it("renders a javascript: source_url as plain text — the section holds exactly one anchor, the https one", async () => {
    const { container } = await load();

    const anchors = Array.from(container.querySelectorAll("a"));
    expect(anchors).toHaveLength(1);
    expect(anchors[0].getAttribute("href")).toBe(
      "https://arc4.com/resources/local-seo-pricing/",
    );
    expect(anchors[0].getAttribute("target")).toBe("_blank");
    expect(anchors[0].getAttribute("rel")).toBe("noreferrer");

    // The hostile URL is still VISIBLE — as inert text, never a link.
    expect(screen.getByText("javascript:alert(document.cookie)")).toBeInTheDocument();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it("renders data:/relative/malformed URLs as text and never as anchors", async () => {
    const { container } = await load();

    const hrefs = Array.from(container.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(["https://arc4.com/resources/local-seo-pricing/"]);
    for (const bad of ["data:", "/relative/path"]) {
      expect(container.querySelector(`a[href^="${bad}"]`)).toBeNull();
    }
    expect(container.querySelector('a[href="/relative/path"]')).toBeNull();
    // A hostile row WITH a label shows the label as text; the URL is not linked anywhere.
    expect(screen.getByText("looks legit")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "looks legit" })).toBeNull();
  });

  it("keeps the null-source row unlinked and the legit row linked (label text preserved)", async () => {
    const { container } = await load();

    const link = screen.getByRole("link", { name: "https://arc4.com/resources/local-seo-pricing/ (F)" });
    expect(link).toHaveAttribute("href", "https://arc4.com/resources/local-seo-pricing/");
    expect(container.querySelectorAll("a")).toHaveLength(1);
  });
});
