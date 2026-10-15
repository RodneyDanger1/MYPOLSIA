/**
 * Wave V12-G1 — dashboard layout store unit tests (the Edit Dashboard closer).
 *
 * Pure store behavior: shipped defaults, hide/show, within-area reorder, bounds,
 * restore, persistence round-trip, sanitize of corrupt stored values, and the
 * localStorage-unavailable path (changes stay live in memory; the state is
 * reported, never swallowed).
 */
import {
  __resetLayoutStoreForTests,
  areaOrder,
  getLayoutSnapshot,
  INITIAL_LAYOUT,
  layoutStorageAvailable,
  moveWidget,
  restoreDefaultLayout,
  sanitizeLayout,
  STORAGE_KEY,
  toggleWidget,
} from "@/components/dashboard/dashboardLayout";

beforeEach(() => {
  window.localStorage.clear();
  __resetLayoutStoreForTests();
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe("defaults", () => {
  it("starts fully visible in the shipped reading order", () => {
    const layout = getLayoutSnapshot();
    expect(layout.hidden).toEqual([]);
    // V16/W3 moved this pin with the table: the Phone card (H-P1 consumer) appended to the left
    // rail — the shipped order only grows.
    expect(areaOrder(layout, "left")).toEqual(["status", "auto_mode", "business", "website", "phone"]);
    expect(areaOrder(layout, "middle")).toEqual(["tasks", "documents", "media"]);
    expect(areaOrder(layout, "right")).toEqual(["social", "outreach", "ads", "caps"]);
    expect(areaOrder(layout, "regions")).toEqual(["activity", "chat"]);
  });
});

describe("toggleWidget", () => {
  it("hides and shows, and persists each step honestly", () => {
    toggleWidget("chat");
    expect(getLayoutSnapshot().hidden).toEqual(["chat"]);
    const raw = window.localStorage.getItem(STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw as string).hidden).toEqual(["chat"]);

    toggleWidget("chat");
    expect(getLayoutSnapshot().hidden).toEqual([]);
    // Back to the shipped default -> the key is removed, not left as a stub.
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("survives a reload (persistence round-trip through localStorage)", () => {
    toggleWidget("media");
    __resetLayoutStoreForTests(); // a fresh process, same browser storage
    expect(getLayoutSnapshot().hidden).toEqual(["media"]);
  });
});

describe("moveWidget", () => {
  it("swaps within its own area only", () => {
    moveWidget("tasks", 1);
    expect(areaOrder(getLayoutSnapshot(), "middle")).toEqual(["documents", "tasks", "media"]);
    // The left rail's shipped order (V16/W3: phone appended) is untouched by a middle-column move.
    expect(areaOrder(getLayoutSnapshot(), "left")).toEqual([
      "status",
      "auto_mode",
      "business",
      "website",
      "phone",
    ]);
  });

  it("is a no-op at the bounds", () => {
    moveWidget("status", -1);
    moveWidget("phone", 1);
    // V16/W3: the bottom-bound probe moved website -> phone with the table — the LAST left-rail
    // item is the one that cannot move down; website now has a neighbour below it.
    expect(areaOrder(getLayoutSnapshot(), "left")).toEqual([
      "status",
      "auto_mode",
      "business",
      "website",
      "phone",
    ]);
  });
});

describe("restoreDefaultLayout", () => {
  it("brings back the shipped order and visibility and clears storage", () => {
    toggleWidget("chat");
    moveWidget("tasks", 1);
    restoreDefaultLayout();
    expect(getLayoutSnapshot()).toEqual(INITIAL_LAYOUT);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});

describe("sanitizeLayout", () => {
  it("drops unknown ids, dedupes, and keeps per-area order of known ids only", () => {
    const dirty = {
      hidden: ["chat", "chat", "nope", 7],
      order: { middle: ["media", "media", "bogus"], left: ["website"], regions: "x" },
    } as unknown;
    expect(sanitizeLayout(dirty)).toEqual({
      hidden: ["chat"],
      order: { middle: ["media"], left: ["website"] },
    });
  });

  it("answers the shipped defaults for garbage", () => {
    expect(sanitizeLayout(null)).toEqual(INITIAL_LAYOUT);
    expect(sanitizeLayout("x")).toEqual(INITIAL_LAYOUT);
    expect(sanitizeLayout({ hidden: "chat" })).toEqual(INITIAL_LAYOUT);
  });

  it("discards an unreadable stored layout instead of half-applying it", () => {
    window.localStorage.setItem(STORAGE_KEY, "{not json");
    expect(getLayoutSnapshot()).toEqual(INITIAL_LAYOUT);
  });
});

describe("storage unavailable", () => {
  it("keeps changes live in memory and reports the state when writes are refused", () => {
    const setItem = jest.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    toggleWidget("chat");
    expect(getLayoutSnapshot().hidden).toEqual(["chat"]);
    expect(layoutStorageAvailable()).toBe(false);
    setItem.mockRestore();
  });

  it("reports unavailability when the initial read throws", () => {
    const getItem = jest.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(getLayoutSnapshot()).toEqual(INITIAL_LAYOUT);
    expect(layoutStorageAvailable()).toBe(false);
    getItem.mockRestore();
  });
});
