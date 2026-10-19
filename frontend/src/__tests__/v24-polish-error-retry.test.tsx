import { act, fireEvent, render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { ...actual.api, get: jest.fn(), post: jest.fn() } };
});

import { api } from "@/lib/api";
import FinancePage from "@/app/finance/page";
import { TasksPreview } from "@/components/dashboard/TasksPreview";
import { PanelError } from "@/components/dashboard/states";
import { ReadError } from "@/components/finance/states";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  mockApiGet.mockReset();
});

describe("shared error states carry a Retry that re-issues the read", () => {
  it("ReadError without a handler stays button-free (auxiliary reads keep the same voice)", () => {
    render(<ReadError label="the thing" message="E_TIMEOUT: no response" />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("ReadError's Retry calls the page's own re-read", () => {
    const onRetry = jest.fn();
    render(<ReadError label="the thing" message="E_TIMEOUT: no response" onRetry={onRetry} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("the finance summary failure offers Retry and re-issues GET /finance/summary", async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path === "/finance/summary") return Promise.reject(new Error("E_NETWORK: down"));
      if (path === "/config") return Promise.resolve({ human_hourly_rate: null });
      if (path === "/finance/planning") return Promise.resolve({});
      if (path === "/economics/context") return Promise.resolve({});
      return Promise.resolve([]);
    });

    render(<FinancePage />);
    await flush();
    expect(await screen.findByText(/Could not load the finance summary/)).toBeTruthy();

    const summaryCalls = () =>
      mockApiGet.mock.calls.filter(([path]) => path === "/finance/summary").length;
    expect(summaryCalls()).toBe(1);

    fireEvent.click(screen.getByTestId("finance-summary-retry"));
    await flush();
    expect(summaryCalls()).toBe(2);
  });

  it("a dashboard panel failure offers Retry and re-issues its own read", async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path === "/tasks?limit=4") return Promise.reject(new Error("E_NETWORK: down"));
      return Promise.resolve([]);
    });

    render(<TasksPreview />);
    await flush();
    expect(await screen.findByText(/Could not load tasks/)).toBeTruthy();
    expect(mockApiGet.mock.calls.filter(([p]) => p === "/tasks?limit=4")).toHaveLength(1);

    fireEvent.click(screen.getByTestId("tasks-preview-retry"));
    await flush();
    expect(mockApiGet.mock.calls.filter(([p]) => p === "/tasks?limit=4")).toHaveLength(2);
  });

  it("PanelError without a handler stays button-free", () => {
    render(<PanelError label="tasks" message="E_NETWORK: down" />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
