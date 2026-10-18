/**
 * WAVE V17 / seat L4 — money on the dashboard obeys the house money law.
 *
 * `moneyText` (components/dashboard/format.ts) printed a raw float: MRR 1234 became `$1234`, and
 * Auto Mode spend 12.5 became `$12.5`. The finance family (`usdText`) prints `$1,234.00` / `$12.50`,
 * and the ads card's own comment names this exact split ("the audit's Q-13 split ... cannot come
 * back") — the dashboard was still doing it. Every money value now goes through the same shape;
 * a server-supplied display STRING is left as it is (it may already carry a symbol or a suffix).
 *
 * RED leg: pre-fix `moneyText(1234)` is "$1234" and `moneyText(12.5)` is "$12.5".
 */
import { moneyText, NOT_MEASURED } from "@/components/dashboard/format";

describe("dashboard moneyText — the finance family's shape", () => {
  it("groups thousands and keeps two decimals for a measured number", () => {
    expect(moneyText(1234)).toBe("$1,234.00");
    expect(moneyText(12.5)).toBe("$12.50");
  });

  it("a measured zero is a reading ($0.00), never the not-measured marker", () => {
    expect(moneyText(0)).toBe("$0.00");
  });

  it("null/absent stays the not-measured marker — never $0.00", () => {
    expect(moneyText(null)).toBe(NOT_MEASURED);
    expect(moneyText(undefined)).toBe(NOT_MEASURED);
  });

  it("a numeric string is money too (the KPI bag is untyped), a display string is left alone", () => {
    expect(moneyText("1234.5")).toBe("$1,234.50");
    expect(moneyText("$1,200")).toBe("$1,200");
    expect(moneyText("not reported")).toBe("$not reported");
  });
});
