/**
 * WAVE V17 / seat L4 — the `/capabilities` nav entry (owner mandate: the read-only agent -> tool ->
 * action map shipped reachable only from Settings).
 *
 * The nav table is the ONE source the MENU dropdown and the Ctrl+K palette both build from, so the
 * claim is pinned here directly: /capabilities is the sixteenth entry, sits in the system cluster
 * after Memory and before Settings (Settings stays last, the file's own convention), and carries a
 * real lucide icon.
 *
 * RED leg (raw output in wave-v17/l4-frontend-sweep/raw/): written before the table change — pre-fix
 * the table holds 15 entries and no /capabilities href, so both pins here fail exactly so.
 *
 * V24/L3 re-pin: the exact href list below moved 16 -> 17 when the Sites route was added (after
 * /ads); every /capabilities assertion above is untouched — Capabilities still sits before Settings.
 */
import { NAV_ITEMS } from "@/components/layout/nav-items";

describe("nav table — the capabilities route (16th)", () => {
  it("carries exactly one /capabilities item, labelled Capabilities, with a lucide icon", () => {
    const items = NAV_ITEMS.filter((item) => item.href === "/capabilities");
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe("Capabilities");
    expect(items[0].icon).toBeDefined();
  });

  it("is the seventeen-route IA, /capabilities in the system cluster before Settings", () => {
    expect(NAV_ITEMS.map((item) => item.href)).toEqual([
      "/dashboard",
      "/agents",
      "/tasks",
      "/planner",
      "/schedules",
      "/social",
      "/outreach",
      "/support",
      "/phone",
      "/ads",
      // V24/L3 moved this list 16 -> 17 in the SAME edit that added the Sites route; the
      // /capabilities position below is unchanged.
      "/sites",
      "/finance",
      "/invoices",
      "/usage",
      "/memory",
      // V17/L4 moved this pin 15 -> 16 in the SAME edit that added the Capabilities route: the
      // read-only capability map is a reference surface, so it sits with Usage/Memory in the
      // system cluster and Settings stays the last item.
      "/capabilities",
      "/settings",
    ]);
    expect(NAV_ITEMS).toHaveLength(17);
  });
});
