import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api } from "@/lib/api";
import MemoryPage, { type Memory } from "@/app/memory/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/**
 * T15d / fix-1 §4 residual: the semantic path serves `MemoryItem`, whose `category` and `title` are
 * `str | None` (`backend/app/schemas/memory.py` — the Chroma metadata may simply not carry them;
 * `memory_service` builds each hit with `meta.get("category")` / `meta.get("title")`).
 *
 * Both fixtures below are declared as the page's exported `Memory` type, so this file stops
 * compiling the moment either side drops the null — that IS the compile-time pin. The DOM
 * assertions pin the render: a null category is the gray "unknown" chip (never an empty chip) and
 * a null title is named "untitled" (never an empty line, never the literal word "null").
 */
const nullFields: Memory = {
  id: "chroma-9",
  category: null,
  title: null,
  content: "the content is always present — content is required on the wire",
  created_at: null,
  fallback: null,
};

const recordedFields: Memory = {
  id: 42,
  category: "strategy",
  title: "alpha plan",
  content: "recorded fields must be untouched by the null fallbacks",
  created_at: null,
  fallback: null,
};

async function searchFor(query: string) {
  const user = userEvent.setup();
  render(<MemoryPage />);
  await user.type(screen.getByPlaceholderText("Search company memory…"), query);
  await user.click(screen.getByRole("button", { name: "Search" }));
}

describe("MemoryPage null-safe category/title (T15d)", () => {
  afterEach(() => mockApiGet.mockReset());

  it("renders a null category as the gray 'unknown' chip, never an empty chip", async () => {
    mockApiGet.mockResolvedValue([nullFields]);

    await searchFor("alpha");

    await screen.findByText("untitled");
    const chip = screen.getByText("unknown");
    expect(chip.className).toContain("bg-gray-700");
    expect(chip.className).not.toContain("bg-indigo");
    expect(screen.queryByText("null")).toBeNull();
  });

  it("renders a null title as 'untitled', never an empty line or the word null", async () => {
    mockApiGet.mockResolvedValue([{ ...nullFields, category: "strategy" }]);

    await searchFor("alpha");

    expect(await screen.findByText("untitled")).toBeInTheDocument();
    // The content is still shown in full — the null title hides nothing.
    expect(screen.getByText(nullFields.content)).toBeInTheDocument();
    expect(screen.queryByText("null")).toBeNull();
  });

  it("leaves a recorded category/title exactly as served", async () => {
    mockApiGet.mockResolvedValue([recordedFields]);

    await searchFor("alpha");

    expect(await screen.findByText("alpha plan")).toBeInTheDocument();
    const chip = screen.getByText("strategy");
    expect(chip.className).toContain("bg-indigo-600/30");
  });
});
