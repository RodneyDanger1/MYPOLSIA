/**
 * WAVE 8 — ERROR-SURFACE TRUTH at the two shared surfaces every page inherits:
 *
 *  1. `lib/api.ts` — a FastAPI REQUEST-VALIDATION refusal (`422 {"detail":[{loc,msg,…}]}`) used to
 *     reach the user as the raw JSON blob in the message.  Measured live 2026-10-29 on the running
 *     app (`POST /invoices {"net_days":999}` -> 422; the body is pydantic's array with NO code),
 *     and the invoice form's own comment claimed the server named an `E_VALIDATION` it never sends.
 *     The client now renders the server's field names + messages, bounded, and never prints JSON —
 *     without inventing a refusal code (the server named none, so the code stays `E_HTTP_<status>`).
 *  2. `app/error.tsx` / `app/global-error.tsx` — a NAMED refusal that reaches a boundary keeps its
 *     code + fix hint (`ApiError.display`); the boundary used to render `error.message` alone.
 */
import { fireEvent, render, screen } from "@testing-library/react";

import { ApiError, api } from "@/lib/api";
import SegmentError from "@/app/error";
import GlobalError from "@/app/global-error";

function mockFetchResponse(status: number, body: string) {
  (global as unknown as { fetch: jest.Mock }).fetch = jest.fn().mockResolvedValue({
    ok: false,
    status,
    statusText: "mock status",
    headers: { get: () => null },
    text: async () => body,
    json: async () => JSON.parse(body),
  });
}

afterEach(() => {
  jest.restoreAllMocks();
});

describe("request-validation refusals are readable (lib/api.ts)", () => {
  it("renders FastAPI's field/message array instead of the JSON blob", async () => {
    // THE LIVE BODY, verbatim (measured 2026-10-29 against POST /api/v1/invoices {"net_days":999}).
    mockFetchResponse(
      422,
      JSON.stringify({
        detail: [
          {
            type: "less_than_equal",
            loc: ["body", "net_days"],
            msg: "Input should be less than or equal to 365",
            input: 999,
            ctx: { le: 365 },
          },
        ],
      }),
    );

    const err = (await api.get("/x").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    // The server named NO code — the client does not invent one.
    expect(err.code).toBe("E_HTTP_422");
    expect(err.display).toBe("E_HTTP_422: net_days: Input should be less than or equal to 365");
    expect(err.display).not.toContain('"detail"');
    expect(err.display).not.toContain('"loc"');
    // The parsed body is kept for callers that need it; the message never prints it.
    expect(err.body).toEqual(
      expect.objectContaining({ detail: expect.any(Array) }),
    );
  });

  it("names several fields, bounded to three, and says how many it did not print", async () => {
    mockFetchResponse(
      422,
      JSON.stringify({
        detail: [
          { loc: ["body", "name"], msg: "Field required" },
          { loc: ["body", "hour"], msg: "Input should be less than or equal to 23" },
          { loc: ["body", "line_items", 0, "quantity"], msg: "Input should be a valid number" },
          { loc: ["body", "when"], msg: "Invalid datetime" },
          { loc: ["body", "email"], msg: "Invalid email" },
        ],
      }),
    );
    const err = (await api.get("/x").catch((e) => e)) as ApiError;
    expect(err.message).toBe(
      "name: Field required; hour: Input should be less than or equal to 23; " +
        "line_items.0.quantity: Input should be a valid number; and 2 more field errors",
    );
  });

  it("never prints the JSON blob even when the array names no usable message", async () => {
    mockFetchResponse(422, JSON.stringify({ detail: [{ loc: ["body"], msg: "" }] }));
    const err = (await api.get("/x").catch((e) => e)) as ApiError;
    expect(err.code).toBe("E_HTTP_422");
    expect(err.message).toBe(
      "the server rejected the request body as invalid, but its validation payload named no field message",
    );
    expect(err.message).not.toContain("{");
  });

  it("caps a hostile per-field message and drops the echoed input entirely", async () => {
    const long = "x".repeat(400);
    mockFetchResponse(
      422,
      JSON.stringify({
        detail: [
          {
            loc: ["body", "api_key"],
            msg: long,
            input: "sk-live-secret-that-must-not-be-printed",
          },
        ],
      }),
    );
    const err = (await api.get("/x").catch((e) => e)) as ApiError;
    expect(err.message.startsWith("api_key: x")).toBe(true);
    expect(err.message.length).toBeLessThan(320);
    expect(err.message).toContain("…");
    expect(err.message).not.toContain("sk-live-secret");
  });

  it("a NAMED refusal still wins over the array path (code + fix hint)", async () => {
    mockFetchResponse(
      422,
      JSON.stringify({
        ok: false,
        error: {
          code: "E_VALIDATION",
          message: "net_days must be <= 365",
          how_to_fix: "use 0-365 whole days",
        },
        detail: [{ loc: ["body", "net_days"], msg: "unused" }],
      }),
    );
    const err = (await api.get("/x").catch((e) => e)) as ApiError;
    expect(err.code).toBe("E_VALIDATION");
    expect(err.display).toBe("E_VALIDATION: net_days must be <= 365 — use 0-365 whole days");
  });
});

describe("the segment error boundary keeps a named refusal's code and fix", () => {
  function silenceConsole() {
    return jest.spyOn(console, "error").mockImplementation(() => undefined);
  }

  it("renders the full ApiError.display under a refusal heading", () => {
    const consoleError = silenceConsole();
    const refusal = new ApiError(
      503,
      "sendgrid: SENDGRID_API_KEY is not configured",
      "E_CONFIG",
      "set SENDGRID_API_KEY in the environment and restart",
    );
    render(<SegmentError error={refusal} reset={jest.fn()} />);

    expect(screen.getByText("The server refused this request")).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong")).toBeNull();
    expect(screen.getByText("Named refusal")).toBeInTheDocument();
    expect(screen.getByText("Refusal detail")).toBeInTheDocument();
    expect(screen.getByTestId("error-message")).toHaveTextContent(
      "E_CONFIG: sendgrid: SENDGRID_API_KEY is not configured — set SENDGRID_API_KEY in the environment and restart",
    );
    consoleError.mockRestore();
  });

  it("a plain render error keeps the existing generic surface (no invented code)", () => {
    const consoleError = silenceConsole();
    render(<SegmentError error={new Error("boom: the real render failure")} reset={jest.fn()} />);

    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByTestId("error-message")).toHaveTextContent("boom: the real render failure");
    expect(screen.getByTestId("error-message")).not.toHaveTextContent("E_");
    consoleError.mockRestore();
  });

  it("never renders an empty refusal box for a blank display (absence, not a shell)", () => {
    const consoleError = silenceConsole();
    const error = Object.assign(new Error("boom"), { display: "   " });
    render(<SegmentError error={error} reset={jest.fn()} />);
    expect(screen.getByTestId("error-message")).toHaveTextContent("boom");
    expect(screen.queryByText("Named refusal")).toBeNull();
    consoleError.mockRestore();
  });

  it("still offers the two real recovery controls on a refusal", () => {
    const consoleError = silenceConsole();
    const reset = jest.fn();
    const error = Object.assign(new Error("x"), { display: "E_CONFIG: nope — fix it" });
    render(<SegmentError error={error} reset={reset} />);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(reset).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Reload the page" })).toBeInTheDocument();
    consoleError.mockRestore();
  });
});

describe("the root boundary keeps a named refusal's code too", () => {
  it("renders display when carried", () => {
    const error = Object.assign(new Error("x"), { display: "E_CONFIG: no approval secret — set it" });
    render(<GlobalError error={error} reset={jest.fn()} />);
    expect(screen.getByTestId("global-error-message")).toHaveTextContent(
      "E_CONFIG: no approval secret — set it",
    );
    expect(screen.getByText("Refusal detail")).toBeInTheDocument();
  });

  it("falls back to the real message when no display is carried", () => {
    render(<GlobalError error={new Error("root layout exploded")} reset={jest.fn()} />);
    expect(screen.getByTestId("global-error-message")).toHaveTextContent("root layout exploded");
    expect(screen.getByText("Error detail")).toBeInTheDocument();
  });
});
