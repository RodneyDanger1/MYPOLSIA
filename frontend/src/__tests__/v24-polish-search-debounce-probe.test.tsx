import { act, fireEvent, render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { ...actual.api, get: jest.fn() } };
});

import { api } from "@/lib/api";
import MemoryPage from "@/app/memory/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/**
 * V24/L5 — the debounce candidate, MEASURED before changing anything.
 *
 * The brief's candidate list assumes the memory search fires per keystroke. It does not: the page
 * is an EXPLICIT search (button / Enter), so typing `revenue` issues ZERO requests and a settled
 * query issues exactly ONE. A 250 ms debounce here would only add latency to a request the user
 * explicitly asked for. This test pins the measurement (0 requests while typing) so the decision
 * is on disk rather than in a claim.
 */
describe("memory search — per-keystroke request count (the debounce candidate)", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiGet.mockResolvedValue([]);
  });

  it("typing fires no request; the settled query fires exactly one", async () => {
    render(<MemoryPage />);
    const input = screen.getByLabelText("Search company memory");

    fireEvent.change(input, { target: { value: "r" } });
    fireEvent.change(input, { target: { value: "re" } });
    fireEvent.change(input, { target: { value: "rev" } });
    fireEvent.change(input, { target: { value: "reve" } });
    fireEvent.change(input, { target: { value: "reven" } });
    fireEvent.change(input, { target: { value: "revenu" } });
    fireEvent.change(input, { target: { value: "revenue" } });

    expect(mockApiGet).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Search" }));
      await Promise.resolve();
    });

    expect(mockApiGet).toHaveBeenCalledTimes(1);
    expect(mockApiGet).toHaveBeenCalledWith(
      "/memory?q=revenue&limit=20",
      expect.objectContaining({ timeoutMs: 120_000 }),
    );
  });
});
