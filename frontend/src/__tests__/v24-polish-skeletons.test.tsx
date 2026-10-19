import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { ...actual.api, get: jest.fn() } };
});

import { api } from "@/lib/api";
import SocialPage from "@/app/social/page";
import SupportPage from "@/app/support/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/** A read that never settles keeps the page on its loading branch for the whole assertion. */
const pending = () => new Promise<never>(() => {});

beforeEach(() => {
  mockApiGet.mockReset();
  mockApiGet.mockImplementation(() => pending() as never);
});

describe("loading skeletons reproduce the loaded structure (no card-count / row-shape jump)", () => {
  it("support: ONE card-surface with four row-shaped lists, plus the reserved count line", () => {
    render(<SupportPage />);

    const skeleton = screen.getByTestId("support-loading");
    const cards = skeleton.querySelectorAll(":scope .card-surface");
    expect(cards).toHaveLength(1);

    const list = cards[0] as HTMLElement;
    expect(list.tagName).toBe("UL");
    expect(list.querySelectorAll(":scope > li")).toHaveLength(4);
    for (const row of Array.from(list.querySelectorAll(":scope > li"))) {
      expect(row.className).toContain("border-b");
      expect(row.className).toContain("px-3");
      expect(row.className).toContain("py-3");
    }

    // The count line above the list is rendered only after the read settles today; reserving its
    // box during loading is what stops the list jumping down when it arrives.
    const reserved = screen.getByTestId("support-count-placeholder");
    expect(reserved.className).toContain("ui-label");
    expect(reserved.textContent?.trim()).toBe("");
  });

  it("social: three card-surface rows shaped like SocialPostCard, not bare h-24 blocks", () => {
    render(<SocialPage />);

    const skeleton = screen.getByTestId("social-loading");
    const rows = skeleton.querySelectorAll('[data-testid="social-skeleton-post"]');
    expect(rows).toHaveLength(3);

    for (const row of Array.from(rows)) {
      expect((row as HTMLElement).className).toContain("card-surface");
      const content = row.querySelector('[data-testid="social-skeleton-content"]');
      const meta = row.querySelector('[data-testid="social-skeleton-meta"]');
      const footer = row.querySelector('[data-testid="social-skeleton-footer"]');
      expect(content).not.toBeNull();
      expect(meta).not.toBeNull();
      expect(footer).not.toBeNull();
      // The footer is separated by the real card's hairline + top padding.
      expect((footer as HTMLElement).className).toContain("border-t");
      expect((footer as HTMLElement).className).toContain("pt-2");
    }
  });
});
