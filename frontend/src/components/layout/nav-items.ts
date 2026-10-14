import {
  BarChart2,
  Bot,
  BriefcaseBusiness,
  Database,
  DollarSign,
  Gauge,
  Mail,
  Megaphone,
  Settings,
  Twitter,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

/**
 * The app's whole navigation, in one place.
 *
 * The live product has no persistent nav rail — its IA lives in the top-right
 * `MENU ▾` dropdown (agent1-live-chrome/DOSSIER.md §1.2/§1.9, cross-cutting A;
 * aid3 MATCH-VS-BEAT C2 forbids re-adding a sidebar). Our clone routes ten
 * pages, so the dropdown carries those ten routes; the labels stay as short as
 * the live menu items.
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
  { href: "/social", label: "Social", icon: Twitter },
  { href: "/outreach", label: "Outreach", icon: Mail },
  { href: "/ads", label: "Ads", icon: Megaphone },
  { href: "/finance", label: "Finance", icon: DollarSign },
  { href: "/usage", label: "Usage", icon: Gauge },
  { href: "/memory", label: "Memory", icon: Database },
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
