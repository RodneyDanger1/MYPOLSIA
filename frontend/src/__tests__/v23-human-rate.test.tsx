import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

/**
 * WAVE V23 / L3 — the Finance page's Human rate card.
 *
 * Wire truth (the backend pinned by `backend/tests/unit/test_v23_human_rate.py`):
 *  - `GET /config` serves `human_hourly_rate: number | null` — null is "no rate was ever set",
 *    never 0;
 *  - `PUT /config {"human_hourly_rate": n}` (n >= 0) stores it; `{"human_hourly_rate": null}`
 *    clears it; the response is the full config row, and the card reflects THE SERVER'S OWN
 *    answer — no optimistic state (a failed PUT must leave the old value on screen);
 *  - the rate is what unlocks the Planning panel's margin template, but only with measured
 *    capacity — the card's copy says exactly that and claims nothing more.
 */

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import { HumanRateCard } from "@/components/finance/HumanRateCard";

const mockGet = api.get as jest.Mock;
const mockPut = api.put as jest.Mock;

afterEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
});

function serveRate(rate: number | null) {
  mockGet.mockImplementation((path: string) => {
    if (path === "/config") return Promise.resolve({ human_hourly_rate: rate });
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

async function renderCard(rate: number | null = null) {
  serveRate(rate);
  render(<HumanRateCard reloadKey={0} />);
  await screen.findByTestId(rate === null ? "human-rate-absent" : "human-rate-value");
}

const rateInput = () => screen.getByLabelText("Human hourly rate (USD per hour)") as HTMLInputElement;

describe("V23/L3 — HumanRateCard (show / absent / set / clear / named error, no optimism)", () => {
  it("shows the stored rate, and reads it from GET /config", async () => {
    await renderCard(85);

    expect(screen.getByTestId("human-rate-value")).toHaveTextContent("$85.00");
    expect(screen.getByTestId("human-rate-value")).toHaveTextContent("per hour");
    expect(mockGet).toHaveBeenCalledWith("/config");
    // A stored 0 is a reading, not an absence — still a value row.
    expect(screen.queryByTestId("human-rate-absent")).toBeNull();
  });

  it("renders the honest absence when no rate is set, with no Clear action", async () => {
    await renderCard(null);

    expect(screen.getByTestId("human-rate-absent")).toHaveTextContent("Not set");
    expect(screen.getByTestId("human-rate-absent")).toHaveTextContent("margin");
    expect(screen.queryByTestId("human-rate-value")).toBeNull();
    expect(screen.queryByRole("button", { name: "Clear rate" })).toBeNull();
    expect(rateInput().value).toBe(""); // absent is an EMPTY box, never 0 or the word null
  });

  it("sets a rate through PUT /config and reflects the server's answer — never the draft", async () => {
    await renderCard(null);
    let resolvePut!: (value: unknown) => void;
    mockPut.mockImplementation(() => new Promise((resolve) => { resolvePut = resolve; }));

    fireEvent.change(rateInput(), { target: { value: "42.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save rate" }));
    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    expect(mockPut).toHaveBeenCalledWith("/config", { human_hourly_rate: 42.5 });

    // While the write is in flight the OLD state is still what is shown (no optimistic flip).
    expect(screen.getByTestId("human-rate-absent")).toBeInTheDocument();
    expect(screen.queryByTestId("human-rate-value")).toBeNull();
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();

    await act(async () => {
      resolvePut({ human_hourly_rate: 42.5 });
    });
    expect(await screen.findByTestId("human-rate-value")).toHaveTextContent("$42.50");
    expect(rateInput().value).toBe(""); // the draft is spent only after the server agreed
  });

  it("clears the rate by sending null and reflects the cleared row", async () => {
    await renderCard(85);
    mockPut.mockResolvedValue({ human_hourly_rate: null });

    fireEvent.click(screen.getByRole("button", { name: "Clear rate" }));
    await waitFor(() => expect(mockPut).toHaveBeenCalledWith("/config", { human_hourly_rate: null }));

    expect(await screen.findByTestId("human-rate-absent")).toHaveTextContent("Not set");
    expect(screen.queryByTestId("human-rate-value")).toBeNull();
    expect(screen.queryByRole("button", { name: "Clear rate" })).toBeNull();
  });

  it("names a refused PUT and keeps the old value on screen", async () => {
    await renderCard(85);
    mockPut.mockRejectedValue(
      new ApiError(503, "the company row could not be written", "E_DB", "retry the save"),
    );

    fireEvent.change(rateInput(), { target: { value: "90" } });
    fireEvent.click(screen.getByRole("button", { name: "Save rate" }));

    const error = await screen.findByTestId("human-rate-error");
    expect(error).toHaveTextContent("E_DB");
    expect(error).toHaveTextContent("the company row could not be written");
    // The failed write changed nothing on screen.
    expect(screen.getByTestId("human-rate-value")).toHaveTextContent("$85.00");
    expect(screen.queryByTestId("human-rate-absent")).toBeNull();
  });

  it("refuses blank and negative drafts BY NAME with no PUT", async () => {
    await renderCard(null);

    fireEvent.click(screen.getByRole("button", { name: "Save rate" }));
    expect(await screen.findByTestId("human-rate-error")).toHaveTextContent("Enter a rate");

    fireEvent.change(rateInput(), { target: { value: "-5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save rate" }));
    expect(await screen.findByTestId("human-rate-error")).toHaveTextContent(
      "greater than or equal to 0",
    );

    expect(mockPut).not.toHaveBeenCalled();
  });

  it("names a failed read instead of drawing a value", async () => {
    mockGet.mockRejectedValue(new ApiError(503, "the database is unreachable", "E_DB"));
    render(<HumanRateCard reloadKey={0} />);

    const error = await screen.findByTestId("human-rate-read-error");
    expect(error).toHaveTextContent("Could not read the human hourly rate");
    expect(error).toHaveTextContent("E_DB");
    expect(screen.queryByTestId("human-rate-value")).toBeNull();
    expect(screen.queryByTestId("human-rate-absent")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save rate" })).toBeNull();
  });
});
