"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";

/**
 * State for the shell's topbar panels (MENU, NOTIFICATIONS, EDIT).
 *
 * This is the WAI-ARIA *disclosure* pattern, not `role="menu"`: the panel holds
 * navigation, and ARIA menus are for application commands. The trigger owns
 * `aria-expanded`/`aria-controls`, the panel follows it in DOM order so Tab
 * moves into it naturally, and the closing behaviours are:
 *
 * - `Escape` anywhere closes the panel and returns focus to the trigger;
 * - a pointer press outside the trigger+panel closes it without stealing focus;
 * - focus moving outside the trigger+panel (Tab away, window blur) closes it.
 *
 * The trigger's own press is deliberately exempt from the outside-press rule so
 * a second click toggles closed instead of close-then-reopen.
 */
export type Popover = {
  /** Is the panel open? */
  open: boolean;
  /** Open/close the panel from the trigger. */
  toggle: () => void;
  /** Close the panel; `restoreFocus` hands focus back to the trigger. */
  close: (restoreFocus?: boolean) => void;
  /** Attach to the `<button>` that opens the panel. */
  triggerRef: React.RefObject<HTMLButtonElement>;
  /** Attach to the panel element. */
  panelRef: React.RefObject<HTMLDivElement>;
  /** `id` for the panel + the trigger's `aria-controls`. */
  panelId: string;
  /** Spread on the element that wraps BOTH trigger and panel. */
  wrapperProps: {
    onBlur: (event: React.FocusEvent<HTMLElement>) => void;
  };
};

export function usePopover(): Popover {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  const close = useCallback((restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) {
      triggerRef.current?.focus();
    }
  }, []);

  const toggle = useCallback(() => {
    setOpen((previous) => !previous);
  }, []);

  useEffect(() => {
    if (!open) return undefined;

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        close(true);
      }
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      // The trigger keeps ownership of its own press so click #2 is a toggle.
      if (triggerRef.current?.contains(target)) return;
      if (panelRef.current?.contains(target)) return;
      close(false);
    }

    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("pointerdown", handlePointerDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("pointerdown", handlePointerDown);
    };
  }, [open, close]);

  const onBlur = useCallback(
    (event: React.FocusEvent<HTMLElement>) => {
      const next = event.relatedTarget;
      if (!(next instanceof Node)) {
        // Focus left the document (window blur) — nothing inside can be mid-use.
        close(false);
        return;
      }
      if (panelRef.current?.contains(next)) return;
      if (triggerRef.current?.contains(next)) return;
      close(false);
    },
    [close],
  );

  return { open, toggle, close, triggerRef, panelRef, panelId, wrapperProps: { onBlur } };
}
