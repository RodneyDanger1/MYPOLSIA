import {
  BarChart2,
  Bot,
  BriefcaseBusiness,
  CalendarClock,
  Database,
  DollarSign,
  FileText,
  Gauge,
  Globe,
  Layers,
  LifeBuoy,
  Mail,
  Megaphone,
  Phone,
  Settings,
  ShieldAlert,
  Target,
  Twitter,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

/**
 * The app's whole navigation, in one place.
 *
 * The live product has no persistent nav rail — its IA lives in the top-right
 * `MENU ▾` dropdown (agent1-live-chrome/DOSSIER.md §1.2/§1.9, cross-cutting A;
 * aid3 MATCH-VS-BEAT C2 forbids re-adding a sidebar). Our clone routes the live pages plus the
 * owner-requested operator surfaces added by V11 unit 2 (Invoices, Schedules, Support) — the
 * count claim moves with the table (eleven -> sixteen; V17/L4 added Capabilities; V24/L3 added
 * Sites, so seventeen; V25/L5 added Ops, so eighteen; V27/L5 added Prospects, so nineteen), labels
 * stay as short as the live menu items.
 */
export type NavItem = {
  /** App Router path. */
  href: string;
  /** Visible label; rendered in the live's mono-ALL-CAPS voice by the consumer. */
  label: string;
  icon: LucideIcon;
};

export const NAV_ITEMS: readonly NavItem[] = [
  { href: "/dashboard", label: "Dashboard", icon: BarChart2 },
  { href: "/agents", label: "Agents", icon: Bot },
  { href: "/tasks", label: "Tasks", icon: BriefcaseBusiness },
  // V9/B5: the planner (plan §9 B1/B5) sits right after Tasks — the two work surfaces.
  { href: "/planner", label: "Planner", icon: Target },
  // V11 unit 2: the schedule CRUD surface (wiring audit F1) — schedules ran via cadence.py with
  // no UI at all until this wave; managed next to the planner that proposes the work.
  { href: "/schedules", label: "Schedules", icon: CalendarClock },
  { href: "/social", label: "Social", icon: Twitter },
  { href: "/outreach", label: "Outreach", icon: Mail },
  // V27/L5: the owner-managed prospect CRM — the WRITABLE sibling of the read-only Outreach
  // surface (add / correct / remove a row), so it sits right after it. The count claim above
  // moves eighteen -> nineteen with this row.
  { href: "/prospects", label: "Prospects", icon: Users },
  // V11 unit 2: the support inbox mirror (wiring audit F4) — the customer_support channel had no
  // in-app view until this wave.
  { href: "/support", label: "Support", icon: LifeBuoy },
  // V16/W3: the owner's phone-link surface (the H-P1 read family: threads, messages, calls and
  // the real export download) — sits with the comms cluster, after the support inbox.
  { href: "/phone", label: "Phone", icon: Phone },
  { href: "/ads", label: "Ads", icon: Megaphone },
  // V24/L3: the website builder — the one surface that writes AND hosts a real page. It sits with
  // the publishing cluster (Ads), before the money/system surfaces; the count claim above moves
  // sixteen -> seventeen with this row.
  { href: "/sites", label: "Sites", icon: Globe },
  { href: "/finance", label: "Finance", icon: DollarSign },
  // V11 unit 2: invoices + dunning are visible billing surfaces (wiring audit F2) — they shipped
  // backend-only in V9; the page sits beside Finance.
  { href: "/invoices", label: "Invoices", icon: FileText },
  { href: "/usage", label: "Usage", icon: Gauge },
  { href: "/memory", label: "Memory", icon: Database },
  // V25/L5: the God Mode cockpit — runs, pending approvals, manual dispatches and the live kill
  // switches in one owner surface (`app/ops/page.tsx`, existing API only). It joins the system
  // cluster before the read-only Capabilities map; Settings stays the last item. The count claim
  // above moves seventeen -> eighteen with this row.
  { href: "/ops", label: "Ops", icon: ShieldAlert },
  // V17/L4: the read-only capability map (agent -> tool -> action, shipped in V16) was reachable
  // only from Settings; it is a reference surface, so it joins the system cluster here and Settings
  // stays the last item (the file's own convention).
  { href: "/capabilities", label: "Capabilities", icon: Layers },
  { href: "/settings", label: "Settings", icon: Settings },
] as const;

/**
 * Is `href` the current route for `pathname`?
 *
 * Matches on SEGMENT boundaries: `/tasks` is current for `/tasks` and
 * `/tasks/123`, but NOT for a future `/tasks-archive`. The retired sidebar used
 * a bare `pathname.startsWith(href)`, which made `/tasks` claim `/tasks-archive`
 * (audit Q-15).
 */
export function isActiveRoute(pathname: string | null | undefined, href: string): boolean {
  if (!pathname) return false;
  return pathname === href || pathname.startsWith(`${href}/`);
}
