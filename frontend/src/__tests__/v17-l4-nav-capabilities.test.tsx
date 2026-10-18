/**
 * WAVE V17 / seat L4 — the `/capabilities` nav entry (owner mandate: the read-only agent -> tool ->
 * action map shipped reachable only from Settings).
 *
 * The nav table is the ONE source the MENU dropdown and the Ctrl+K palette both build from, so the
 * claim is pinned here directly: /capabilities is a 16th entry, sits in the system cluster after
 * Memory and before Settings (Settings stays last, the file's own convention), and carries a real
 * lucide icon.
 *
 * RED leg (raw output in wave-v17/l4-frontend-sweep/raw/): written before the table change — pre-fix
 * the table holds 15 entries and no /capabilities href, so both pins here fail exactly so.
 */
import { NAV_ITEMS } from "@/components/layout/nav-items";

describe("nav table — the capabilities route (16th)", () => {
  it("carries exactly one /capabilities item, labelled Capabilities, with a lucide icon", () => {
    const items = NAV_ITEMS.filter((item) => item.href === "/capabilities");
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe("Capabilities");
    expect(items[0].icon).toBeDefined();
  });

  it("is the sixteen-route IA, /capabilities in the system cluster before Settings", () => {
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
    expect(NAV_ITEMS).toHaveLength(16);
  });
});
