"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronDown } from "lucide-react";

import { NAV_ITEMS, isActiveRoute } from "@/components/layout/nav-items";
import { usePopover } from "@/components/layout/usePopover";
import { cn } from "@/components/ui/cn";

/**
 * Top-right `MENU ▾` — the app's whole navigation.
 *
 * Shaped after the live dropdown (agent1-live-chrome/DOSSIER.md §1.9): a white
 * panel with a 1px dark border and a drop shadow, right-aligned under the
 * trigger, monospace ALL-CAPS rows with a leading icon. The live menu's active
 * row inverts to black-on-white; ours does the same AND carries two non-colour
 * cues — a `▸` marker glyph and bold weight — plus `aria-current="page"`, so the
 * current page is announced and is not signalled by colour alone (audit Q-15).
 */
export function MenuNav() {
  const pathname = usePathname();
  const { open, toggle, close, triggerRef, panelRef, panelId, wrapperProps } = usePopover();

  return (
    <div className="relative" {...wrapperProps}>
      <button
        ref={triggerRef}
        type="button"
        className="btn btn-bevel"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={toggle}
        data-testid="nav-menu-trigger"
      >
        Menu
        <ChevronDown size={12} aria-hidden="true" />
      </button>

      {open ? (
        <div
          ref={panelRef}
          id={panelId}
          className="panel-pop absolute right-0 z-50 mt-1 w-56 py-1"
        >
          <nav aria-label="Main">
            <ul>
              {NAV_ITEMS.map(({ href, label, icon: Icon }) => {
                const active = isActiveRoute(pathname, href);
                return (
                  <li key={href}>
                    <Link
                      href={href}
                      aria-current={active ? "page" : undefined}
                      onClick={() => close(false)}
                      className={cn(
                        "ui-label flex items-center gap-2 py-1.5 pl-2 pr-3 text-ink",
                        active
                          ? "bg-band font-bold text-band-ink hover:bg-band"
                          : "hover:bg-surface-strong",
                      )}
                    >
                      <span
                        aria-hidden="true"
                        className="w-3 shrink-0 text-center text-[10px]"
                      >
                        {active ? "▸" : ""}
                      </span>
                      <Icon size={13} aria-hidden="true" className="shrink-0" />
                      {label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </nav>
        </div>
      ) : null}
    </div>
  );
}
