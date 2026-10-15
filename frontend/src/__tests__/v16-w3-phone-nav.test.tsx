/**
 * WAVE V16 / seat W3 — the 15th route: `/phone` (the H-P1 consumer family's nav item).
 *
 * The nav table (`components/layout/nav-items.ts`) is the ONE source the MENU dropdown and the
 * Ctrl+K palette both build from, so its content is pinned here directly: the phone route ships
 * as a 15th entry, sits in the comms cluster after Support, and carries a real lucide icon. The
 * consuming surfaces' own count pins move with the table in the same edit
 * (`u1-shell.test.tsx`: MENU_ROUTES + the tab-walk count).
 *
 * RED leg (raw output in wave-v16/w3-phone-ui/raw/): written before the table change — pre-fix
 * the table holds 14 entries and no /phone href, so both pins here fail exactly so.
 */
import { NAV_ITEMS } from "@/components/layout/nav-items";

describe("nav table — the phone route (15th)", () => {
  it("carries exactly one /phone item, labelled Phone, with an icon", () => {
    const items = NAV_ITEMS.filter((item) => item.href === "/phone");
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe("Phone");
    expect(items[0].icon).toBeDefined();
  });

  it("is the frozen fifteen-route IA, /phone in the comms cluster after Support", () => {
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
      "/settings",
    ]);
  });
});
