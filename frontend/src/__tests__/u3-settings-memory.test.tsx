import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import MemoryPage, { type Memory } from "@/app/memory/page";
import SettingsPage, { type SettingsConfig } from "@/app/settings/page";
import { DeepSeekKeySection } from "@/components/settings/DeepSeekKeySection";

const mockGet = api.get as jest.Mock;
const mockPut = api.put as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockDelete = api.delete as jest.Mock;

afterEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
  mockPost.mockReset();
  mockDelete.mockReset();
});

// ---------------------------------------------------------------------------------------------
// SETTINGS — settings.md §5 acceptance criteria (wave U3 lane L2)
// ---------------------------------------------------------------------------------------------

const wireConfig: SettingsConfig = {
  name: "Polsia",
  mission: null,
  vision: null,
  description: null,
  target_market: null,
  value_prop: null,
  website_url: null,
  industry: null,
  product_type: null,
  timezone: null,
  daily_cycle_hour: null,
};

const seededConfig: SettingsConfig = {
  ...wireConfig,
  industry: "Software",
  timezone: "America/Chicago",
  daily_cycle_hour: 6,
};

/** The page mounts DeepSeekKeySection only when its tab opens, and that section reads its own
 *  endpoint — both reads are served here so a settings-only test cannot accidentally depend on
 *  the provider read firing early. */
function serveReads(config: SettingsConfig | Promise<SettingsConfig>) {
  mockGet.mockImplementation((path: string) => {
    if (path === "/config") return Promise.resolve(config);
    if (path === "/config/deepseek-key") {
      return Promise.resolve({ configured: false, source: "none" as const, masked: "" });
    }
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

const configGets = () => mockGet.mock.calls.filter(([p]) => p === "/config").length;
const deepseekGets = () =>
  mockGet.mock.calls.filter(([p]) => p === "/config/deepseek-key").length;

async function renderSettings(config: SettingsConfig | Promise<SettingsConfig> = seededConfig) {
  serveReads(config);
  const view = render(<SettingsPage />);
  await screen.findByRole("tab", { name: "Company Profile" });
  return view;
}

const COMPANY_LABELS = [
  "Company Name",
  "Industry",
  "Product Type",
  "Website URL",
  "Mission",
  "Vision",
  "Description",
  "Target Market",
  "Value Proposition",
] as const;
const SCHEDULER_LABELS = ["Timezone", "Morning Cycle Hour (company-local)"] as const;
const HOUR_LABEL = "Morning Cycle Hour (company-local)";

describe("settings — labels associated (Q-14, settings.md ACC 1)", () => {
  it("clicking each of the 11 field labels focuses its input", async () => {
    const user = userEvent.setup();
    await renderSettings();

    for (const label of COMPANY_LABELS) {
      const input = screen.getByLabelText(label) as HTMLInputElement;
      await user.click(screen.getByText(label));
      expect(document.activeElement).toBe(input);
    }

    await user.click(screen.getByRole("tab", { name: "Scheduler" }));
    for (const label of SCHEDULER_LABELS) {
      const input = screen.getByLabelText(label) as HTMLInputElement;
      await user.click(screen.getByText(label));
      expect(document.activeElement).toBe(input);
    }
  });

  it("every input is reachable by its label (htmlFor/id pairing)", async () => {
    await renderSettings();
    for (const label of COMPANY_LABELS) {
      expect(screen.getByLabelText(label)).toBeInstanceOf(HTMLInputElement);
    }
    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));
    for (const label of SCHEDULER_LABELS) {
      expect(screen.getByLabelText(label)).toBeInstanceOf(HTMLInputElement);
    }
  });
});

describe("settings — underline tabs (spec R2, live §2.1)", () => {
  it("renders all four tabs, exposes tab semantics, and switches panels", async () => {
    await renderSettings();

    const tabs = screen.getAllByRole("tab");
    // V7/S4 added the Emails tab (3rd); the End-key assertion below still lands on the LAST tab.
    expect(tabs.map((t) => t.textContent)).toEqual([
      "Company Profile",
      "Scheduler",
      "Emails",
      "DeepSeek Key",
    ]);
    expect(screen.getByRole("tab", { name: "Company Profile" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tabpanel")).toBeInTheDocument();
    expect(screen.getByLabelText("Company Name")).toBeInTheDocument();
    expect(screen.queryByLabelText("Timezone")).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));
    expect(screen.getByRole("tab", { name: "Scheduler" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Timezone")).toBeInTheDocument();
    expect(screen.queryByLabelText("Company Name")).toBeNull();
  });

  it("supports keyboard tab navigation (ArrowRight/ArrowLeft/Home/End)", async () => {
    await renderSettings();
    const company = screen.getByRole("tab", { name: "Company Profile" });

    company.focus();
    fireEvent.keyDown(company, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Scheduler" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Scheduler" }));

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "Company Profile" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "End" });
    expect(screen.getByRole("tab", { name: "DeepSeek Key" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Home" });
    expect(screen.getByRole("tab", { name: "Company Profile" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("reads the deepseek status only when its tab first opens (lazy, live §2.4)", async () => {
    await renderSettings();

    expect(configGets()).toBe(1);
    expect(deepseekGets()).toBe(0);

    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));
    expect(deepseekGets()).toBe(0);

    fireEvent.click(screen.getByRole("tab", { name: "DeepSeek Key" }));
    await screen.findByText("Not configured");
    expect(deepseekGets()).toBe(1);

    // No control besides the config save and the DeepSeek section fires a request (ACC 6).
    expect(mockPut).not.toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();
  });
});

describe("settings — clearable number + honest drafts (Q-18, ACC 2)", () => {
  it("clearing the hour box sends null — never the previous number, never midnight", async () => {
    await renderSettings();
    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));

    const hour = screen.getByLabelText(HOUR_LABEL) as HTMLInputElement;
    expect(hour.value).toBe("6");

    fireEvent.change(hour, { target: { value: "" } });
    expect(hour.value).toBe("");

    mockPut.mockResolvedValue({ ...seededConfig, daily_cycle_hour: null });
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const [path, body] = mockPut.mock.calls[0];
    expect(path).toBe("/config");
    expect(body).toEqual({ daily_cycle_hour: null });
    // The cleared box stays clear after the save lands (server truth: NULL).
    await waitFor(() => expect((screen.getByLabelText(HOUR_LABEL) as HTMLInputElement).value).toBe(""));
  });

  it("typing an invalid hour shows a message, keeps the draft, and blocks the save", async () => {
    await renderSettings();
    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));

    const hour = screen.getByLabelText(HOUR_LABEL) as HTMLInputElement;
    fireEvent.change(hour, { target: { value: "abc" } });

    expect(hour.value).toBe("abc"); // never silently reverted
    expect(hour).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/whole number/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save Settings" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("dirty-gates the save and sends exactly the changed fields", async () => {
    await renderSettings();

    const save = screen.getByRole("button", { name: "Save Settings" });
    expect(save).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Industry"), { target: { value: "Widgets" } });
    expect(save).toBeEnabled();

    mockPut.mockResolvedValue({ ...seededConfig, industry: "Widgets" });
    fireEvent.click(save);

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const [, body] = mockPut.mock.calls[0];
    expect(body).toEqual({ industry: "Widgets" });
    await screen.findByRole("status");
  });

  it("clearing the timezone sends '' (the server normalises it to 'no value')", async () => {
    await renderSettings();
    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));

    fireEvent.change(screen.getByLabelText("Timezone"), { target: { value: "" } });
    mockPut.mockResolvedValue({ ...seededConfig, timezone: null });
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const [, body] = mockPut.mock.calls[0];
    expect(body).toEqual({ timezone: "" });
  });
});

describe("settings — honest load/save states (Q-24, ACC 4/5)", () => {
  it("renders a skeleton with aria-busy while the config read is in flight — never bare text", () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    const { container } = render(<SettingsPage />);

    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(screen.getByText(/Loading company configuration/)).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save Settings" })).toBeNull();
  });

  it("renders the refused save as role=alert with the server's own code and hint, form still editable", async () => {
    await renderSettings();
    mockPut.mockRejectedValue(
      new ApiError(
        422,
        "timezone: Value error, 'Not/AZone' is not a known IANA timezone",
        "E_VALIDATION",
        "use an IANA zone name like America/Chicago",
      ),
    );

    fireEvent.change(screen.getByLabelText("Industry"), { target: { value: "Changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_VALIDATION");
    expect(alert).toHaveTextContent("use an IANA zone name like America/Chicago");
    expect(screen.queryByRole("status")).toBeNull();

    // The form stays editable (a refusal is not a hang).
    fireEvent.change(screen.getByLabelText("Industry"), { target: { value: "Changed again" } });
    expect((screen.getByLabelText("Industry") as HTMLInputElement).value).toBe("Changed again");
    expect(screen.getByRole("button", { name: "Save Settings" })).toBeEnabled();
  });

  it("keeps the success banner after a second save inside 3 s, and clears its timer on unmount", async () => {
    jest.useFakeTimers();
    const clearSpy = jest.spyOn(global, "clearTimeout");
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    try {
      serveReads(seededConfig);
      mockPut.mockImplementation((_path: string, body: Record<string, unknown>) =>
        Promise.resolve({ ...seededConfig, ...body }),
      );
      const { unmount } = render(<SettingsPage />);
      await act(async () => {});

      fireEvent.change(screen.getByLabelText("Industry"), { target: { value: "First" } });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
      });
      expect(screen.getByRole("status")).toHaveTextContent("Settings saved successfully");

      act(() => {
        jest.advanceTimersByTime(2000);
      });
      fireEvent.change(screen.getByLabelText("Industry"), { target: { value: "Second" } });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
      });

      // 4 s after the first save the banner must still be up: the second save reset the timer.
      act(() => {
        jest.advanceTimersByTime(2000);
      });
      expect(screen.getByRole("status")).toHaveTextContent("Settings saved successfully");

      // 3 s after the SECOND save it goes (and the timer that would fire was real).
      act(() => {
        jest.advanceTimersByTime(1001);
      });
      expect(screen.queryByRole("status")).toBeNull();

      // Unmount with a pending timer clears it — no state update escapes.
      fireEvent.change(screen.getByLabelText("Industry"), { target: { value: "Third" } });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
      });
      const before = clearSpy.mock.calls.length;
      unmount();
      expect(clearSpy.mock.calls.length).toBeGreaterThan(before);
      act(() => {
        jest.runOnlyPendingTimers();
      });
      expect(errorSpy).not.toHaveBeenCalled();
    } finally {
      clearSpy.mockRestore();
      errorSpy.mockRestore();
      jest.useRealTimers();
    }
  });

  it("names the reason a failed load and offers no fake form", async () => {
    mockGet.mockImplementation((path: string) =>
      path === "/config"
        ? Promise.reject(
            new ApiError(404, "company configuration has not been seeded yet", "E_NOT_FOUND", "run the seed step"),
          )
        : Promise.resolve({ configured: false, source: "none" as const, masked: "" }),
    );
    render(<SettingsPage />);

    expect(await screen.findByText("Failed to load config")).toBeInTheDocument();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("E_NOT_FOUND");
    expect(alert).toHaveTextContent("run the seed step");
    expect(screen.queryByRole("button", { name: "Save Settings" })).toBeNull();
  });

  it("renders an absent-key payload as empty boxes, never the literal word undefined", async () => {
    // The wire type says `| null`; the runtime decides. A payload missing nine keys must not paint
    // "undefined" into nine boxes (and the one key it does carry must render as itself).
    await renderSettings({ name: "Polsia" } as unknown as SettingsConfig);

    expect((screen.getByLabelText("Company Name") as HTMLInputElement).value).toBe("Polsia");
    for (const label of COMPANY_LABELS.filter((entry) => entry !== "Company Name")) {
      expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe("");
    }
    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));
    expect((screen.getByLabelText(HOUR_LABEL) as HTMLInputElement).value).toBe("");
    expect(screen.queryByText(/undefined/)).toBeNull();
    expect(screen.queryByText(/null/)).toBeNull();
  });
});

describe("settings — no phantom settings (GAP-NO-ENDPOINT, ACC 6)", () => {
  it("renders none of the live-only surfaces and no dark legacy classes", async () => {
    const { container } = await renderSettings();
    const text = container.textContent ?? "";
    const bannedWords = ["EMAILS", "BILLING", "PAUSE", "DEACTIVATE", "AUTO TOP UP", "CONTENT LANGUAGE"];
    const found = bannedWords.filter((word) => text.includes(word));
    expect(found).toEqual([]); // none of the endpoint-less live surfaces may exist here
    expect(container.innerHTML).not.toContain("bg-gray-800");
    expect(container.innerHTML).not.toContain("text-white");
    expect(mockPut).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------------------------
// SETTINGS — DeepSeek key lifecycle (Q-19)
// ---------------------------------------------------------------------------------------------

describe("deepseek key section — honest statuses + destructive confirm (Q-19)", () => {
  const STORED = { configured: true, source: "stored" as const, masked: "sk-…1234" };
  const NONE = { configured: false, source: "none" as const, masked: "…" };

  it("shows a skeleton with aria-busy while the status read is in flight", () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    const { container } = render(<DeepSeekKeySection />);
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(screen.getByText(/Checking the key status/)).toBeInTheDocument();
  });

  it("renders a failed SAVE as role=alert, never as a neutral status", async () => {
    mockGet.mockResolvedValue(NONE);
    mockPut.mockRejectedValue(new ApiError(422, "api_key: blank", "E_VALIDATION", "paste the key"));

    render(<DeepSeekKeySection />);
    const input = await screen.findByLabelText("DeepSeek API key");
    fireEvent.change(input, { target: { value: "sk-test-not-a-real-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not save the key.");
    expect(alert).toHaveTextContent("E_VALIDATION");
    expect(alert.className).toContain("text-alert");
  });

  it("renders a successful save as a neutral status, not an alert", async () => {
    mockGet.mockResolvedValue(NONE);
    mockPut.mockResolvedValue(STORED);

    render(<DeepSeekKeySection />);
    fireEvent.change(await screen.findByLabelText("DeepSeek API key"), {
      target: { value: "sk-test-not-a-real-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Saved.");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders a failed VERIFY as role=alert with the real reason", async () => {
    mockGet.mockResolvedValue(STORED);
    mockPost.mockResolvedValue({
      ok: false,
      detail: "HTTP 401 from GET https://api.deepseek.com/models: Authentication Fails",
    });

    render(<DeepSeekKeySection />);
    fireEvent.click(await screen.findByRole("button", { name: "Verify" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("HTTP 401 from GET https://api.deepseek.com/models");
    expect(alert).not.toHaveTextContent("Verified");
  });

  it("Render the failed REMOVE as role=alert", async () => {
    mockGet.mockResolvedValue(STORED);
    mockDelete.mockRejectedValue(new ApiError(500, "storage is unwritable", "E_STORAGE", "check the key store"));

    render(<DeepSeekKeySection />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove key" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not remove the stored key.");
    expect(alert).toHaveTextContent("E_STORAGE");
  });

  it("requires a confirm click before DELETE fires, and Escape cancels it", async () => {
    mockGet.mockResolvedValue(STORED);
    mockDelete.mockResolvedValue(NONE);

    render(<DeepSeekKeySection />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    // No request yet — the confirm is on screen instead.
    expect(mockDelete).not.toHaveBeenCalled();
    const confirm = await screen.findByRole("group", { name: /confirm removing the stored key/i });
    expect(confirm).toHaveTextContent(/cannot be recovered/i);

    // Escape cancels, no request.
    fireEvent.keyDown(confirm, { key: "Escape" });
    expect(screen.queryByRole("button", { name: "Remove key" })).toBeNull();
    expect(mockDelete).not.toHaveBeenCalled();

    // Confirm path: the second click is the one that deletes.
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove key" }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/config/deepseek-key"));
    await screen.findByText("Not configured");
  });

  it("renders an unknown source word honestly instead of an empty badge", async () => {
    mockGet.mockResolvedValue({ configured: true, source: "file", masked: "sk-…zzzz" });

    render(<DeepSeekKeySection />);
    // The badge must still name something the operator can read.
    expect(await screen.findByTestId("deepseek-source")).not.toHaveTextContent(/^\s*$/);
    expect(screen.getByTestId("deepseek-source").textContent).toContain("file");
  });

  it("renders a configured-but-unmasked status with an explicit marker, not an empty box", async () => {
    mockGet.mockResolvedValue({ configured: true, source: "env", masked: "" });

    render(<DeepSeekKeySection />);
    expect(await screen.findByTestId("deepseek-masked")).toHaveTextContent("—");
  });
});

// ---------------------------------------------------------------------------------------------
// MEMORY — Q-16 / Q-11 / honest states (wave U3 lane L2)
// ---------------------------------------------------------------------------------------------

const memoryRow: Memory = {
  id: 42,
  category: "strategy",
  title: "alpha plan",
  content: "the content is always present — content is required on the wire",
  created_at: null,
  fallback: null,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function searchFor(query: string) {
  const user = userEvent.setup();
  render(<MemoryPage />);
  await user.type(screen.getByLabelText("Search company memory"), query);
  await user.click(screen.getByRole("button", { name: "Search" }));
}

describe("memory — accessible name (Q-16)", () => {
  it("gives the search input a real label, reachable by name, and clicking the label focuses it", async () => {
    const user = userEvent.setup();
    render(<MemoryPage />);

    const input = screen.getByRole("textbox", { name: "Search company memory" });
    expect(input).toBe(screen.getByLabelText("Search company memory"));

    await user.click(screen.getByText("Search company memory"));
    expect(document.activeElement).toBe(input);
  });

  it("does not fetch before the operator searches, and an empty query fires nothing", async () => {
    render(<MemoryPage />);
    expect(mockGet).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(mockGet).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Search company memory"), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(mockGet).not.toHaveBeenCalled();
  });
});

describe("memory — Enter shares the disabled guard, out-of-order results cannot render (Q-11)", () => {
  it("a second Enter while a search is in flight fires no second request", async () => {
    const pending = deferred<Memory[]>();
    mockGet.mockReturnValue(pending.promise);

    render(<MemoryPage />);
    const input = screen.getByLabelText("Search company memory");
    fireEvent.change(input, { target: { value: "alpha" } });

    fireEvent.keyDown(input, { key: "Enter" });
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Searching…" })).toBeDisabled();

    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mockGet).toHaveBeenCalledTimes(1); // the guard, both paths

    await act(async () => {
      pending.resolve([memoryRow]);
    });
    expect(await screen.findByText("alpha plan")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Search" })).toBeEnabled();

    // After the settle a fresh search is allowed and replaces the results.
    mockGet.mockResolvedValue([{ ...memoryRow, id: 43, title: "beta plan" }]);
    fireEvent.change(input, { target: { value: "beta" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("beta plan")).toBeInTheDocument();
    expect(screen.queryByText("alpha plan")).toBeNull();
  });

  it("renders a refused in-flight search honestly — the failure named, no results, no fake empty", async () => {
    const first = deferred<Memory[]>();
    mockGet.mockReturnValueOnce(first.promise);

    render(<MemoryPage />);
    const input = screen.getByLabelText("Search company memory");
    fireEvent.change(input, { target: { value: "alpha" } });
    fireEvent.keyDown(input, { key: "Enter" });

    // The single in-flight request refuses; the UI must report that refusal honestly (there is no
    // newer request to supersede it, and no results row may be manufactured).
    await act(async () => {
      first.reject(new ApiError(500, "vector store unreachable", "E_DB", "retry once the index is up"));
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_DB");
    expect(alert.textContent).toContain("retry once the index is up");
    expect(screen.queryByText("fresh plan")).toBeNull();
    expect(screen.queryByText(/No entries matched/)).toBeNull();
  });
});

describe("memory — the warm search is preserved and its options are the real API shape", () => {
  it("asks for the 120 s timeout and the page's limit", async () => {
    mockGet.mockResolvedValue([]);
    await searchFor("alpha");

    expect(mockGet).toHaveBeenCalledTimes(1);
    const [url, init] = mockGet.mock.calls[0];
    expect(url).toBe("/memory?q=alpha&limit=20");
    expect(init).toEqual({ timeoutMs: 120000 });
  });

  it("says the index is warming past the threshold without claiming a failure", async () => {
    jest.useFakeTimers();
    try {
      const pending = deferred<Memory[]>();
      mockGet.mockReturnValue(pending.promise);

      render(<MemoryPage />);
      fireEvent.change(screen.getByLabelText("Search company memory"), {
        target: { value: "alpha" },
      });
      fireEvent.keyDown(screen.getByLabelText("Search company memory"), { key: "Enter" });

      expect(screen.queryByText(/warms up/i)).toBeNull();
      act(() => {
        jest.advanceTimersByTime(10001);
      });
      expect(screen.getByText(/warms up/i)).toBeInTheDocument();
      expect(screen.queryByRole("alert")).toBeNull();

      await act(async () => {
        pending.resolve([]);
      });
      expect(screen.queryByText(/warms up/i)).toBeNull();
      expect(screen.getByText("No entries matched that search. Try a broader word — agents record memory as they work.")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });
});

describe("memory — honest three states + hostile payloads", () => {
  it("shows guidance before any search and never a fake result", () => {
    render(<MemoryPage />);
    expect(
      screen.getByText(/Search company memory to see what the agents have recorded/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("names a failed search (ApiError display) and shows no results", async () => {
    mockGet.mockRejectedValue(
      new ApiError(503, "vector store unavailable", "E_DB", "retry once the index is back"),
    );
    await searchFor("alpha");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Search failed");
    expect(alert).toHaveTextContent("E_DB");
    expect(alert).toHaveTextContent("retry once the index is back");
    expect(screen.queryByText(/No entries matched/)).toBeNull();
  });

  it("prints the no-match state only for a successful empty read (with guidance, Q-26)", async () => {
    mockGet.mockResolvedValue([]);
    await searchFor("nothing");

    expect(
      await screen.findByText(
        "No entries matched that search. Try a broader word — agents record memory as they work.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders results with category, fallback, date placeholders and the truncation label", async () => {
    const twenty = Array.from({ length: 20 }, (_, i) => ({
      ...memoryRow,
      id: i + 1,
      title: `plan ${i + 1}`,
    }));
    mockGet.mockResolvedValue(twenty);
    await searchFor("plan");

    expect(await screen.findByText("plan 1")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Showing the first 20 matches — this semantic search serves no total, so more may exist.",
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText("date unknown").length).toBe(20);
  });

  it("does not claim truncation for a short page", async () => {
    mockGet.mockResolvedValue([memoryRow]);
    await searchFor("alpha");
    expect(await screen.findByText("alpha plan")).toBeInTheDocument();
    expect(screen.queryByText(/Showing the first/)).toBeNull();
  });

  it("renders a hostile row (null id/title/content, empty category) without crashing or lying", async () => {
    mockGet.mockResolvedValue([
      { id: null, category: "", title: null, content: null, created_at: null, fallback: null },
    ]);
    await searchFor("alpha");

    expect(await screen.findByText("untitled")).toBeInTheDocument();
    expect(screen.getByText("unknown")).toBeInTheDocument();
    expect(screen.getByText("No content recorded.")).toBeInTheDocument();
    expect(screen.getByText("date unknown")).toBeInTheDocument();
    expect(screen.queryByText(/null/)).toBeNull();
  });

  it("refuses to present a non-list payload as results or as an empty search", async () => {
    mockGet.mockResolvedValue(null);
    await searchFor("alpha");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Search failed");
    expect(alert).toHaveTextContent(/null/);
    expect(screen.queryByText(/No entries matched/)).toBeNull();
  });

  it("keeps the degradation marker visible on results (a keyword scan must not pass as semantic)", async () => {
    mockGet.mockResolvedValue([{ ...memoryRow, fallback: "sql_someday" }]);
    await searchFor("alpha");

    expect(await screen.findByText("degraded result (sql_someday)")).toBeInTheDocument();
  });

  it("uses no dark legacy classes", async () => {
    const { container } = render(<MemoryPage />);
    expect(container.innerHTML).not.toContain("bg-gray-800");
    expect(container.innerHTML).not.toContain("text-white");
  });
});
