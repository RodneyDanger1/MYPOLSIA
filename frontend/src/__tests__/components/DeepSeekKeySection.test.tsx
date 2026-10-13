import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("@/lib/api", () => ({
  api: {
    get: jest.fn(),
    put: jest.fn(),
    post: jest.fn(),
    delete: jest.fn(),
  },
}));

import { api } from "@/lib/api";
import { DeepSeekKeySection } from "@/components/settings/DeepSeekKeySection";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;
const mockApiPut = api.put as jest.MockedFunction<typeof api.put>;
const mockApiPost = api.post as jest.MockedFunction<typeof api.post>;
const mockApiDelete = api.delete as jest.MockedFunction<typeof api.delete>;

const STORED_KEY = "sk-rd9-frontend-test-9999-8888-7777-1234";
const STORED_STATUS = { configured: true, source: "stored" as const, masked: "sk-…1234" };
const ENV_STATUS = { configured: true, source: "env" as const, masked: "sk-…abcd" };
const NONE_STATUS = { configured: false, source: "none" as const, masked: "…" };

describe("DeepSeekKeySection", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPut.mockReset();
    mockApiPost.mockReset();
    mockApiDelete.mockReset();
  });

  it("shows the masked key and the stored-source label", async () => {
    mockApiGet.mockResolvedValue(STORED_STATUS);

    render(<DeepSeekKeySection />);

    expect(await screen.findByText("Configured (stored in app)")).toBeInTheDocument();
    expect(screen.getByTestId("deepseek-masked")).toHaveTextContent("sk-…1234");
    // Stored: no password field until "Change" is pressed.
    expect(screen.queryByLabelText("DeepSeek API key")).toBeNull();
    expect(screen.getByRole("button", { name: "Change" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });

  it("distinguishes an environment-configured key", async () => {
    mockApiGet.mockResolvedValue(ENV_STATUS);

    render(<DeepSeekKeySection />);

    expect(await screen.findByText("Configured via environment")).toBeInTheDocument();
    expect(screen.getByTestId("deepseek-masked")).toHaveTextContent("sk-…abcd");
    // Nothing is stored, so no Change/Remove — but the owner can still store an override.
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
    expect(screen.getByLabelText("DeepSeek API key")).toHaveAttribute("type", "password");
  });

  it("shows 'Not configured' with a password input and a disabled save button", async () => {
    mockApiGet.mockResolvedValue(NONE_STATUS);

    render(<DeepSeekKeySection />);

    expect(await screen.findByText("Not configured")).toBeInTheDocument();
    expect(screen.getByTestId("deepseek-masked")).toHaveTextContent("…");
    const input = screen.getByLabelText("DeepSeek API key");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");
    expect(screen.getByRole("button", { name: "Save key" })).toBeDisabled();
  });

  it("saves a key, shows the masked result and keeps the raw key out of the DOM", async () => {
    mockApiGet.mockResolvedValue(NONE_STATUS);
    mockApiPut.mockResolvedValue(STORED_STATUS);

    const { container } = render(<DeepSeekKeySection />);
    const input = await screen.findByLabelText("DeepSeek API key");

    fireEvent.change(input, { target: { value: STORED_KEY } });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));

    await waitFor(() =>
      expect(mockApiPut).toHaveBeenCalledWith("/config/deepseek-key", { api_key: STORED_KEY }),
    );
    expect(await screen.findByText("Configured (stored in app)")).toBeInTheDocument();
    // The draft is cleared after the save: the raw key must not linger in the DOM.
    expect(container.textContent).not.toContain(STORED_KEY);
    expect(screen.queryByLabelText("DeepSeek API key")).toBeNull();
  });

  it("never submits a blank key", async () => {
    mockApiGet.mockResolvedValue(NONE_STATUS);

    render(<DeepSeekKeySection />);
    const input = await screen.findByLabelText("DeepSeek API key");

    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));

    expect(mockApiPut).not.toHaveBeenCalled();
  });

  it("removes the stored key and reports the resulting state", async () => {
    mockApiGet.mockResolvedValue(STORED_STATUS);
    mockApiDelete.mockResolvedValue(NONE_STATUS);

    render(<DeepSeekKeySection />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    await waitFor(() => expect(mockApiDelete).toHaveBeenCalledWith("/config/deepseek-key"));
    expect(await screen.findByText("Not configured")).toBeInTheDocument();
  });

  it("shows the REAL verify failure — never a friendly fake success", async () => {
    mockApiGet.mockResolvedValue(STORED_STATUS);
    mockApiPost.mockResolvedValue({
      ok: false,
      detail: "HTTP 401 from GET https://api.deepseek.com/models: Authentication Fails",
    });

    render(<DeepSeekKeySection />);
    fireEvent.click(await screen.findByRole("button", { name: "Verify" }));

    const result = await screen.findByTestId("deepseek-verify-result");
    expect(result).toHaveTextContent("Not verified");
    expect(result).toHaveTextContent("HTTP 401 from GET https://api.deepseek.com/models");
    expect(result).not.toHaveTextContent("Verified —");
  });

  it("reports a successful verify with the server's own detail", async () => {
    mockApiGet.mockResolvedValue(STORED_STATUS);
    mockApiPost.mockResolvedValue({
      ok: true,
      detail: "HTTP 200 from GET https://api.deepseek.com/models — key accepted; 2 model(s)",
    });

    render(<DeepSeekKeySection />);
    fireEvent.click(await screen.findByRole("button", { name: "Verify" }));

    const result = await screen.findByTestId("deepseek-verify-result");
    expect(result).toHaveTextContent("Verified —");
    expect(result).toHaveTextContent("key accepted");
  });

  it("verifies an unsaved draft as the candidate (verify-before-save)", async () => {
    mockApiGet.mockResolvedValue(NONE_STATUS);
    mockApiPost.mockResolvedValue({ ok: false, detail: "HTTP 401 — candidate rejected" });

    render(<DeepSeekKeySection />);
    const input = await screen.findByLabelText("DeepSeek API key");
    fireEvent.change(input, { target: { value: "sk-rd9-candidate-draft-0001" } });

    fireEvent.click(screen.getByRole("button", { name: "Verify" }));

    await waitFor(() =>
      expect(mockApiPost).toHaveBeenCalledWith("/config/deepseek-key/verify", {
        api_key: "sk-rd9-candidate-draft-0001",
      }),
    );
  });

  it("shows a load failure honestly instead of pretending the key is unset", async () => {
    mockApiGet.mockRejectedValue(new Error("E_NETWORK: request timed out after 15000ms"));

    render(<DeepSeekKeySection />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the key status. E_NETWORK: request timed out after 15000ms",
    );
  });
});
