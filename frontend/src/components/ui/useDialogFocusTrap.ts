"use client";

import { useEffect, useRef, type RefObject } from "react";

/**
 * The modal keyboard law, in ONE place: Tab never leaves the panel, and Escape closes the dialog
 * from ANYWHERE — including after focus has left it.
 *
 * WHY Escape is bound to `document` and not to the panel: a React `onKeyDown` on the panel only
 * fires when the event's target is inside it. A mousedown on the dialog's own non-focusable text
 * moves focus to `<body>`, and two of the seven dialogs had no Tab trap at all — in both states a
 * panel-scoped handler never sees the key, so the modal cannot be closed by keyboard (F-40 /
 * A2A3-01; measured 1 of 4 focus positions for a panel handler, 4 of 4 for a document one).
 * `CommandPalette` records the same reason for the same listener, and `usePopover` binds Escape
 * on `document` too.
 *
 * Focus IN and focus RETURN deliberately stay with each dialog: they legitimately differ (the task
 * sheet and the palette return to a captured opener ref, the documents overlay hands the return to
 * `DailyReportPanel`, the editor captures `activeElement`), and a shared hook that also moved
 * focus would fight all four.
 *
 * The selector and the `tabIndex >= 0` filter are the four hand-rolled traps' own set
 * (`NewTaskDialog`, `DocumentsOverlay`, `ActivityStrip`, `PhoneMedia`). The filter is not
 * cosmetic: a roving-tabindex control (the `Tabs` primitive) renders its inactive tabs as
 * `<button tabindex="-1">`, which matches `button` but is NOT in the browser's tab order — without
 * the filter the wrap target would be an element Tab can never reach.
 */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusableItems(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => !element.hasAttribute("disabled") && element.tabIndex >= 0,
  );
}

/**
 * @param panelRef the dialog panel (the element carrying `role="dialog"`).
 * @param onClose  close the dialog. Read through a ref, so an inline arrow does not re-bind the
 *                 document listener on every parent render.
 * @param open     whether the dialog is currently open. Components that return `null` while
 *                 closed (the dashboard editor) cannot rely on mount timing, so they pass it.
 */
export function useDialogFocusTrap(
  panelRef: RefObject<HTMLElement>,
  onClose: () => void,
  open = true,
): void {
  const closeRef = useRef(onClose);

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return undefined;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        // The dialog owns Escape while it is open: stop here so a second overlay beneath (the
        // command palette listens on `window`) does not also close on the same key press.
        event.stopPropagation();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const panel = panelRef.current;
      if (!panel) return;
      const items = focusableItems(panel);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (event.shiftKey) {
        if (active === first || !panel.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last || !panel.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [panelRef, open]);
}
