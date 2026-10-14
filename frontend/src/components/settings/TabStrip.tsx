"use client";

import { useRef } from "react";

import { cn } from "@/components/ui";

/**
 * The live Settings modal's tab strip (agent1-live-chrome/DOSSIER.md §2.1, measured): one 32 px
 * row of mono-ALL-CAPS labels with ~19-20 px gaps and a 1 px hairline under the row; the ACTIVE
 * cue is a black underline exactly the width of its label (so the tab keeps zero horizontal
 * padding and draws a 2 px bottom border), not a filled pill.
 *
 * WAI-ARIA tabs pattern: `role=tablist/tab`, `aria-selected`, roving `tabIndex` (only the active
 * tab is in the tab order), `aria-controls` on the active tab, and Left/Right/Home/End move the
 * selection AND focus (automatic activation — the panel switches with the selection).
 */
export type TabDef = {
  /** Stable id fragment, also used for `aria-controls`/`aria-labelledby` wiring. */
  id: string;
  /** Visible label — rendered ALL-CAPS by `.ui-label`, sentence case in the DOM. */
  label: string;
};

export function TabStrip({
  idPrefix,
  tabs,
  active,
  onSelect,
  ariaLabel,
}: {
  idPrefix: string;
  tabs: readonly TabDef[];
  active: string;
  onSelect: (id: string) => void;
  ariaLabel: string;
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const moveTo = (index: number) => {
    const next = tabs[index];
    if (!next) return;
    onSelect(next.id);
    refs.current[next.id]?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    const index = tabs.findIndex((tab) => tab.id === active);
    if (index < 0) return;
    if (event.key === "ArrowRight") {
      event.preventDefault();
      moveTo((index + 1) % tabs.length);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      moveTo((index - 1 + tabs.length) % tabs.length);
    } else if (event.key === "Home") {
      event.preventDefault();
      moveTo(0);
    } else if (event.key === "End") {
      event.preventDefault();
      moveTo(tabs.length - 1);
    }
  };

  return (
    <div role="tablist" aria-label={ariaLabel} className="flex gap-5 border-b border-line">
      {tabs.map((tab) => {
        const isActive = tab.id === active;
        return (
          <button
            key={tab.id}
            ref={(element) => {
              refs.current[tab.id] = element;
            }}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${tab.id}`}
            aria-selected={isActive}
            aria-controls={isActive ? `${idPrefix}-panel-${tab.id}` : undefined}
            tabIndex={isActive ? 0 : -1}
            onClick={() => onSelect(tab.id)}
            onKeyDown={onKeyDown}
            className={cn(
              "ui-label -mb-px border-b-2 px-0 pb-2 pt-1",
              isActive
                ? "border-band text-ink"
                : "border-transparent text-ink-muted hover:text-ink",
            )}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}
