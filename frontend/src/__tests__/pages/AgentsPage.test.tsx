import { fireEvent, render, screen } from "@testing-library/react";

jest.mock("@/hooks/useAgentStatus", () => ({ useAgentStatus: jest.fn() }));
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { useAgentStatus } from "@/hooks/useAgentStatus";
import AgentsPage from "@/app/agents/page";

const mockUseAgentStatus = useAgentStatus as jest.MockedFunction<typeof useAgentStatus>;
const mockApiPost = api.post as jest.MockedFunction<typeof api.post>;

const statuses = [
  { agent_type: "finance", last_run_status: "completed", last_run_at: null, tasks_today: 2, tasks_total: 5 },
  { agent_type: "social_media", last_run_status: null, last_run_at: null, tasks_today: 0, tasks_total: 1 },
];

describe("AgentsPage", () => {
  afterEach(() => {
    mockUseAgentStatus.mockReset();
    mockApiPost.mockReset();
  });

  it("names a failed /agents/status poll instead of rendering an empty agent list", () => {
    mockUseAgentStatus.mockReturnValue({ statuses: [], loading: false, error: "E_NETWORK: request timed out" });

    render(<AgentsPage />);

    expect(screen.getByRole("alert")).toHaveTextContent("Could not load agent status: E_NETWORK: request timed out");
    expect(screen.queryByRole("button", { name: "Run Now" })).toBeNull();
    expect(screen.queryByText("Finance")).toBeNull();
  });

  it("renders the agent list when the poll succeeded", () => {
    mockUseAgentStatus.mockReturnValue({ statuses, loading: false, error: null });

    render(<AgentsPage />);

    expect(screen.getByText("finance")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Run Now" })).toHaveLength(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders a refused trigger as an ERROR banner, not the success-styled one", async () => {
    mockUseAgentStatus.mockReturnValue({ statuses, loading: false, error: null });
    mockApiPost.mockRejectedValue(new ApiError(503, "the task queue is unreachable", "E_REDIS", "start the broker"));

    render(<AgentsPage />);
    fireEvent.click(screen.getAllByRole("button", { name: "Run Now" })[0]);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_REDIS");
    expect(alert).toHaveTextContent("start the broker");
    expect(alert.className).toContain("border-red-500");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("keeps a queued confirmation in the non-error style", async () => {
    mockUseAgentStatus.mockReturnValue({ statuses, loading: false, error: null });
    mockApiPost.mockResolvedValue({ message: "Task #7 queued" });

    render(<AgentsPage />);
    fireEvent.click(screen.getAllByRole("button", { name: "Run Now" })[0]);

    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Task #7 queued");
    expect(banner.className).toContain("border-indigo-500");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
