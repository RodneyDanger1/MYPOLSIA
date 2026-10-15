/**
 * V14 / v2-sec — U2-04: a `javascript:` (or `data:`) `source_url` must NEVER become a rendered
 * `href`. React 18 does not block `javascript:` URLs and the citation values are free-form
 * research output — pre-fix `<a href={claim.source_url}>` set the attribute verbatim
 * (`wave-v13/u2-security/FINDINGS.md` U2-04, probe `site_html_injection` sibling).
 */
import { render } from "@testing-library/react";

import { CandidateCard } from "@/components/planner/CandidateCard";
import type { PlannerCandidate } from "@/components/planner/usePlanner";

function candidate(sourceUrl: string): PlannerCandidate {
  return {
    id: "cand-1",
    name: "Probe Co",
    one_liner: "A one-liner.",
    buyer: "small businesses",
    price_band: [100, 200],
    channel: "search",
    unit_cost_sketch: "$10/serve",
    rubric: {
      demand: 3,
      margin: 3,
      competition: 2,
      agent_fit: 3,
      time_to_first_dollar: 2,
      owner_fit: 3,
      total: 16,
    },
    gates: { cm_positive: true, budget_fit: true, hours_fit: null },
    claims: [
      { claim: "A cited number.", source_url: sourceUrl, fetched_at: null, quote: null, verified: true },
    ],
  };
}

function renderCard(sourceUrl: string): HTMLElement {
  const { container } = render(
    <CandidateCard candidate={candidate(sourceUrl)} confirming={false} onConfirm={() => {}} />,
  );
  return container;
}

describe("U2-04 planner claim links", () => {
  it("renders a javascript: source_url as plain text, never an href", () => {
    const container = renderCard("javascript:alert(1)");
    expect(container.querySelectorAll('a[href^="javascript:"]')).toHaveLength(0);
    expect(container.querySelectorAll("a")).toHaveLength(0);
    expect(container.textContent).toContain("javascript:alert(1)");
  });

  it("renders a data: source_url as plain text too", () => {
    const container = renderCard("data:text/html,<script>alert(1)</script>");
    expect(container.querySelectorAll("a")).toHaveLength(0);
    expect(container.textContent).toContain("data:text/html");
  });

  it("keeps a real http(s) claim link working", () => {
    const container = renderCard("https://northstar.example/proof");
    const link = container.querySelector("a");
    expect(link).not.toBeNull();
    expect(link?.getAttribute("href")).toBe("https://northstar.example/proof");
    expect(link?.getAttribute("rel")).toBe("noreferrer");
  });
});
