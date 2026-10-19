import { fireEvent, render, screen, waitFor } from "@testing-library/react";

/**
 * WAVE V23 / L1 — Settings → Emails: the cycle-report email toggle.
 *
 * The setting already exists on the wire (`company_config.cycle_report_email`, NOT NULL server
 * default FALSE — backend/app/models/company.py:73; served by `CompanyConfigResponse` and accepted
 * by `CompanyConfigUpdate` — backend/app/schemas/company.py:104,179) and the daily cycle already
 * sends on it (`notify_owner_cycle_report`, backend/app/services/notification_service.py:292-352:
 * one email per morning and evening report, after the report committed, to
 * `notification_email`; the skip/failure is recorded as an activity row). What was missing was
 * the control. This suite pins the frozen interface:
 *
 *  - read GET /config and render the switch OFF when the value is false / ON when true;
 *  - toggling PUTs exactly `{cycle_report_email: <bool>}` — one key, a JSON boolean;
 *  - NO optimistic UI: while the PUT is in flight the switch still shows the old value, and the
 *    SERVER's response (not the click) decides the new one;
 *  - a refused PUT keeps the old value and shows the named error inline (the shared
 *    `readErrorText` / `ApiError.display` sentence — code + how_to_fix), never a success claim;
 *  - an unreadable GET is named instead of rendering a fabricated OFF;
 *  - no storage and no console chatter.
 */

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), put: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { EmailsSection, NOTIFY_ROWS, type NotifyKey } from "@/components/settings/EmailsSection";

const mockGet = api.get as jest.MockedFunction<typeof api.get>;
const mockPut = api.put as jest.MockedFunction<typeof api.put>;

const SWITCH_NAME = "Email me each cycle report";

const notifyValues = Object.fromEntries(NOTIFY_ROWS.map((row) => [row.key, false])) as Record<
  NotifyKey,
  boolean
>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function renderSection(config: { cycle_report_email?: boolean } = {}) {
  mockGet.mockResolvedValue(config);
  render(<EmailsSection values={notifyValues} onChange={jest.fn()} />);
  return screen.findByRole("switch", { name: SWITCH_NAME });
}

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
});

describe("V23/L1 — the cycle-report toggle reads its value from GET /config", () => {
  it("renders OFF when the config says false", async () => {
    const control = await renderSection({ cycle_report_email: false });

    expect(mockGet).toHaveBeenCalledWith("/config");
    expect(control).toHaveAttribute("aria-checked", "false");
  });

  it("renders ON when the config says true", async () => {
    const control = await renderSection({ cycle_report_email: true });

    expect(control).toHaveAttribute("aria-checked", "true");
  });

  it("names an unreadable GET instead of rendering a fabricated OFF", async () => {
    mockGet.mockRejectedValue(
      new ApiError(
        503,
        "company configuration has not been seeded yet",
        "E_CONFIG",
        "run the seed step",
      ),
    );

    render(<EmailsSection values={notifyValues} onChange={jest.fn()} />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_CONFIG");
    expect(alert).toHaveTextContent("run the seed step");
    expect(screen.queryByRole("switch", { name: SWITCH_NAME })).toBeNull();
  });
});

describe("V23/L1 — writing cycle_report_email is a real PUT whose answer drives the switch", () => {
  it("turning it ON PUTs exactly {cycle_report_email: true}, holds the old value, then reflects the server", async () => {
    const control = await renderSection({ cycle_report_email: false });
    const pending = deferred<unknown>();
    mockPut.mockReturnValue(pending.promise);

    fireEvent.click(control);

    expect(mockPut).toHaveBeenCalledTimes(1);
    const [path, body] = mockPut.mock.calls[0];
    expect(path).toBe("/config");
    expect(body).toEqual({ cycle_report_email: true });
    expect(Object.keys(body as Record<string, unknown>)).toEqual(["cycle_report_email"]);
    expect(typeof (body as { cycle_report_email: unknown }).cycle_report_email).toBe("boolean");

    // No optimistic UI: the request is still in flight and the switch still shows the OLD value.
    expect(control).toHaveAttribute("aria-checked", "false");
    expect(control).toBeDisabled();

    pending.resolve({ cycle_report_email: true });

    await waitFor(() => expect(control).toHaveAttribute("aria-checked", "true"));
    expect(control).toBeEnabled();
  });

  it("turning it OFF PUTs exactly {cycle_report_email: false} and reflects the server", async () => {
    const control = await renderSection({ cycle_report_email: true });
    mockPut.mockResolvedValue({ cycle_report_email: false });

    fireEvent.click(control);

    await waitFor(() => expect(control).toHaveAttribute("aria-checked", "false"));
    expect(mockPut).toHaveBeenCalledTimes(1);
    const [path, body] = mockPut.mock.calls[0];
    expect(path).toBe("/config");
    expect(body).toEqual({ cycle_report_email: false });
    expect(Object.keys(body as Record<string, unknown>)).toEqual(["cycle_report_email"]);
  });

  it("reflects the SERVER's answer even when it disagrees with the request", async () => {
    const control = await renderSection({ cycle_report_email: false });
    // The click asks for true; the row the server answers with is still false. The UI must show
    // what the server says, not what was clicked.
    mockPut.mockResolvedValue({ cycle_report_email: false });

    fireEvent.click(control);

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(control).toBeEnabled());
    expect(control).toHaveAttribute("aria-checked", "false");
  });

  it("a refused PUT keeps the old value and names the refusal inline", async () => {
    const control = await renderSection({ cycle_report_email: true });
    mockPut.mockRejectedValue(
      new ApiError(
        503,
        "company configuration has not been seeded yet",
        "E_CONFIG",
        "run the seed step",
      ),
    );

    fireEvent.click(control);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_CONFIG");
    expect(alert).toHaveTextContent("company configuration has not been seeded yet");
    expect(alert).toHaveTextContent("run the seed step");
    // The old state survives the refusal — no flip, no silent revert, no success banner.
    expect(control).toHaveAttribute("aria-checked", "true");
    expect(control).toBeEnabled();
  });

  it("leaves storage alone and never logs to the console", async () => {
    const getItem = jest.spyOn(Storage.prototype, "getItem");
    const setItem = jest.spyOn(Storage.prototype, "setItem");
    const removeItem = jest.spyOn(Storage.prototype, "removeItem");
    const log = jest.spyOn(console, "log").mockImplementation(() => {});
    const warn = jest.spyOn(console, "warn").mockImplementation(() => {});
    const error = jest.spyOn(console, "error").mockImplementation(() => {});
    const info = jest.spyOn(console, "info").mockImplementation(() => {});
    const debug = jest.spyOn(console, "debug").mockImplementation(() => {});

    try {
      const control = await renderSection({ cycle_report_email: false });
      mockPut.mockResolvedValue({ cycle_report_email: true });

      fireEvent.click(control);
      await waitFor(() => expect(control).toHaveAttribute("aria-checked", "true"));

      expect(getItem).not.toHaveBeenCalled();
      expect(setItem).not.toHaveBeenCalled();
      expect(removeItem).not.toHaveBeenCalled();
      expect(log).not.toHaveBeenCalled();
      expect(warn).not.toHaveBeenCalled();
      expect(error).not.toHaveBeenCalled();
      expect(info).not.toHaveBeenCalled();
      expect(debug).not.toHaveBeenCalled();
    } finally {
      getItem.mockRestore();
      setItem.mockRestore();
      removeItem.mockRestore();
      log.mockRestore();
      warn.mockRestore();
      error.mockRestore();
      info.mockRestore();
      debug.mockRestore();
    }
  });
});
