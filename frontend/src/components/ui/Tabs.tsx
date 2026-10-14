"use client";

import { useRef } from "react";

import { cn } from "@/components/ui/cn";

/**
 * Segmented BOXED tabs (wave-u1 spec `sheets-and-composer.md` §2, the Documents
 * `ALL/DOCUMENTS/REPORTS` treatment): active segment white with a 1px border, inactive
 * light-grey fill. The keyboard behaviour is the Settings TabStrip's pattern
 * (`settings/TabStrip.tsx`), reused as behaviour: `role="tablist"`/`role="tab"`,
 * `aria-selected` on the active tab, roving `tabIndex` (only the active tab is in the
 * tab order), ArrowLeft/ArrowRight wrap, Home/End jump, automatic activation —
 * arrows move selection + focus and activate; Enter/Space on the focused tab also
 * activates (a safe no-op when it is already the active tab).
 * Recipe: `globals.css` `.seg-tabs` / `.seg-tab`.
 */
export type TabItem<T extends string> = {
  value: T;
  label: string;
};

export type TabsProps<T extends string> = {
  items: TabItem<T>[];
  value: T;
  onChange: (v: T) => void;
  ariaLabel: string;
};

export function Tabs<T extends string>({ items, value, onChange, ariaLabel }: TabsProps<T>) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const moveTo = (index: number) => {
    const next = items[index];
    if (!next) return;
    onChange(next.value);
    refs.current[next.value]?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    const index = items.findIndex((item) => item.value === value);
    if (index < 0) return;
    if (event.key === "ArrowRight") {
      event.preventDefault();
      moveTo((index + 1) % items.length);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      moveTo((index - 1 + items.length) % items.length);
    } else if (event.key === "Home") {
      event.preventDefault();
      moveTo(0);
    } else if (event.key === "End") {
      event.preventDefault();
      moveTo(items.length - 1);
    }
  };

  return (
    <div role="tablist" aria-label={ariaLabel} className="seg-tabs">
      {items.map((item) => {
        const isActive = item.value === value;
        return (
          <button
            key={item.value}
            ref={(element) => {
              refs.current[item.value] = element;
            }}
            type="button"
            role="tab"
            aria-selected={isActive}
            tabIndex={isActive ? 0 : -1}
            onClick={() => onChange(item.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                if (item.value !== value) onChange(item.value);
                return;
              }
              onKeyDown(event);
            }}
            className={cn("seg-tab", "ui-label", isActive && "seg-tab-active")}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
