/** V29 currency follow-ups: the finance tables' cents formatter and the invoice line-item currency. */
import { moneyCentsText } from "@/components/finance/format";

describe("moneyCentsText", () => {
  it("keeps the exact USD shape", () => {
    expect(moneyCentsText(123455, "USD")).toBe("$1,234.55");
  });

  it("renders the row's own code for a non-USD amount", () => {
    expect(moneyCentsText(123455, "EUR")).toBe("EUR 1,234.55");
    expect(moneyCentsText(100, "jpy")).toBe("JPY 1.00");
  });

  it("keeps the not-measured marker for an absent value", () => {
    expect(moneyCentsText(null, "EUR")).toBe("—");
    expect(moneyCentsText(undefined, null)).toBe("—");
  });
});
