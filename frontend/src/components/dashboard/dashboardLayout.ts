"use client";

import { useSyncExternalStore } from "react";

/**
 * The dashboard layout store (wave V12-G1 — the Edit Dashboard closer).
 *
 * WHAT THE ORIGINAL HAS: an editable dashboard. `EDIT` in the navbar opens the
 * editor; every widget carries a show/hide switch, widgets can be reordered, and
 * "Restore default layout" brings back the starting selection and order
 * (help.polsia.com/add-dashboard-capabilities, fetched 2026-10-15; agent1
 * live-UI DOSSIER §1.2 for the EDIT control itself).
 *
 * WHAT THIS IS: the same feature, browser-local. The live product syncs a layout
 * to the account; no server API here stores one yet (`GET/PUT /api/v1/config`
 * carries company fields only — backend/app/schemas/company.py), so persistence is
 * one versioned `localStorage` key and the sheet SAYS so in its own copy. Server
 * persistence is HANDOFF H1 of wave-v12/g1-product/GAP-LEDGER.md §6; when it
 * lands, only `load`/`persist` change — the shape below is the seam.
 *
 * Honesty rules kept: an unreadable stored layout is DISCARDED to defaults
 * (never half-applied), unknown widget ids are dropped (a rename cannot
 * resurrect a dead widget), and a localStorage failure is reported, not
 * swallowed — changes then last until reload and the sheet says that too.
 */

export type WidgetId =
  | "status"
  | "auto_mode"
  | "business"
  | "website"
  | "phone"
  | "tasks"
  | "documents"
  | "media"
  | "social"
  | "outreach"
  | "ads"
  | "caps"
  | "activity"
  | "chat"
  // V29/L5: the SIGNALS card (fixed RSS/Atom allowlist). Added at the END of the union and of
  // `WIDGETS` so a saved layout from an earlier build keeps its order and gains the card last.
  | "signals";

export type AreaId = "left" | "middle" | "right" | "regions";

export type WidgetMeta = { id: WidgetId; area: AreaId; label: string };

/**
 * The dashboard's widgets in their default reading order — the exact composition
 * `app/dashboard/page.tsx` mounts. `label` is the widget face the live product
 * ships (Twitter / Email / Meta Ads / Documents / Media, DOSSIER §1.4–1.7).
 */
export const WIDGETS: readonly WidgetMeta[] = [
  { id: "status", area: "left", label: "Polsia status" },
  { id: "auto_mode", area: "left", label: "Auto Mode" },
  { id: "business", area: "left", label: "Business" },
  { id: "website", area: "left", label: "Website" },
  // V16/W3: the Phone card (the dashboard's H-P1 consumer — unread / newest threads / missed
  // calls today, or the honest unavailable state). Appended after Website so the default reading
  // order only grows; a saved arrangement from before this build keeps its order and gains the
  // card at its default position (`areaOrder`).
  { id: "phone", area: "left", label: "Phone" },
  { id: "tasks", area: "middle", label: "Tasks" },
  { id: "documents", area: "middle", label: "Documents" },
  { id: "media", area: "middle", label: "Media" },
  { id: "social", area: "right", label: "Twitter" },
  { id: "outreach", area: "right", label: "Email" },
  { id: "ads", area: "right", label: "Meta Ads" },
  { id: "caps", area: "right", label: "Send caps" },
  { id: "activity", area: "regions", label: "Activity feed" },
  { id: "chat", area: "regions", label: "Chat rail" },
  // V29/L5: the SIGNALS card — headlines from the fixed keyless feed allowlist (HN official RSS
  // + TechCrunch). Appended LAST so a saved arrangement only grows; `areaOrder` places it after
  // the send-caps card in the right column.
  { id: "signals", area: "right", label: "Signals" },
];

/** Editor section order (the live editor groups its widget list; ours groups by
 *  dashboard column). */
export const AREAS: readonly AreaId[] = ["left", "middle", "right", "regions"];

export const AREA_LABEL: Record<AreaId, string> = {
  left: "Left rail",
  middle: "Middle column",
  right: "Right column",
  regions: "Full-height regions",
};

export type DashboardLayout = {
  /** Widget ids currently switched off (the live editor's OFF switch). */
  hidden: WidgetId[];
  /** Per-area order; an absent area means the default reading order. */
  order: Partial<Record<AreaId, WidgetId[]>>;
};

/** One versioned key. Bump the version to retire old saved shapes. */
export const STORAGE_KEY = "polsia.dashboard.layout.v1";

/** The shipped dashboard: everything visible, default order. */
export const INITIAL_LAYOUT: DashboardLayout = { hidden: [], order: {} };

const WIDGET_IDS: readonly string[] = WIDGETS.map((w) => w.id);

function defaultOrder(area: AreaId): WidgetId[] {
  return WIDGETS.filter((w) => w.area === area).map((w) => w.id);
}

/**
 * Validate any stored value into a layout this build understands: unknown ids
 * dropped, duplicates deduped, per-area order filtered to that area's ids. Any
 * non-object / wrong-typed part becomes its default — a corrupt store renders
 * the shipped dashboard, never a half-applied one.
 */
export function sanitizeLayout(value: unknown): DashboardLayout {
  if (typeof value !== "object" || value === null) return INITIAL_LAYOUT;
  const raw = value as { hidden?: unknown; order?: unknown };

  const hidden = Array.isArray(raw.hidden)
    ? [...new Set(raw.hidden.filter((x): x is WidgetId => typeof x === "string" && WIDGET_IDS.includes(x)))]
    : [];

  const order: Partial<Record<AreaId, WidgetId[]>> = {};
  if (typeof raw.order === "object" && raw.order !== null) {
    for (const area of AREAS) {
      const list = (raw.order as Record<string, unknown>)[area];
      if (!Array.isArray(list)) continue;
      const valid = [...new Set(list.filter((x): x is WidgetId => typeof x === "string" && defaultOrder(area).includes(x as WidgetId)))];
      if (valid.length > 0) order[area] = valid;
    }
  }

  return { hidden, order };
}

/** The full id list for one area: the owner's saved arrangement first, then every
 *  widget the arrangement does not mention (a widget added in a later build lands
 *  in its default position instead of disappearing). */
export function areaOrder(layout: DashboardLayout, area: AreaId): WidgetId[] {
  const defaults = defaultOrder(area);
  const saved = layout.order[area] ?? [];
  const kept = saved.filter((id) => defaults.includes(id));
  const missing = defaults.filter((id) => !kept.includes(id));
  return [...kept, ...missing];
}

/* ------------------------------------------------------------------ store -- */

let current: DashboardLayout = INITIAL_LAYOUT;
let loaded = false;
let storageAvailable = true;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function persist(): void {
  try {
    if (typeof window === "undefined") return;
    const isDefault = current.hidden.length === 0 && Object.keys(current.order).length === 0;
    if (isDefault) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, JSON.stringify(current));
  } catch {
    // Privacy modes can refuse writes. The layout stays live in memory for this
    // session; the flag makes the sheet say so (never a silent lie about saving).
    storageAvailable = false;
  }
}

function load(): void {
  try {
    if (typeof window === "undefined") return;
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw !== null) current = sanitizeLayout(JSON.parse(raw));
  } catch {
    // Unreadable (JSON.parse) or unavailable (getItem throw) — either way the
    // shipped defaults render and the flag reports the state.
    storageAvailable = false;
  }
}

export function getLayoutSnapshot(): DashboardLayout {
  if (!loaded) {
    loaded = true;
    load();
  }
  return current;
}

/** Server render gets the shipped defaults, so prerendered HTML is stable. */
export function getLayoutServerSnapshot(): DashboardLayout {
  return INITIAL_LAYOUT;
}

export function subscribeLayout(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** True when the browser let us read and write; false = session-only changes. */
export function layoutStorageAvailable(): boolean {
  getLayoutSnapshot();
  return storageAvailable;
}

export function setLayout(next: DashboardLayout): void {
  current = sanitizeLayout(next);
  loaded = true;
  persist();
  emit();
}

export function toggleWidget(id: WidgetId): void {
  const layout = getLayoutSnapshot();
  const hidden = layout.hidden.includes(id)
    ? layout.hidden.filter((x) => x !== id)
    : [...layout.hidden, id];
  setLayout({ ...layout, hidden });
}

/** Move a widget one step within its own area (the editor's up/down controls). */
export function moveWidget(id: WidgetId, delta: -1 | 1): void {
  const layout = getLayoutSnapshot();
  const meta = WIDGETS.find((w) => w.id === id);
  if (!meta) return;
  const order = areaOrder(layout, meta.area);
  const from = order.indexOf(id);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= order.length) return;
  const next = order.slice();
  [next[from], next[to]] = [next[to], next[from]];
  setLayout({ ...layout, order: { ...layout.order, [meta.area]: next } });
}

export function restoreDefaultLayout(): void {
  setLayout(INITIAL_LAYOUT);
}

export function useDashboardLayout(): DashboardLayout {
  return useSyncExternalStore(subscribeLayout, getLayoutSnapshot, getLayoutServerSnapshot);
}

/** Test seam: drop module state so each suite starts from the shipped defaults. */
export function __resetLayoutStoreForTests(): void {
  current = INITIAL_LAYOUT;
  loaded = false;
  storageAvailable = true;
  listeners.clear();
}
