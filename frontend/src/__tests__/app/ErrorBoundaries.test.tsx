import { fireEvent, render, screen } from "@testing-library/react";

import SegmentError from "@/app/error";
import GlobalError from "@/app/global-error";

/**
 * L5-02: the app router had NO error boundaries, so an unhandled render error produced a
 * blank page.  These tests pin the replacement surfaces: the real error message is shown,
 * the retry button calls the framework's `reset`, and a reload escape hatch exists.
 */
describe("app/error.tsx (segment boundary)", () => {
  it("renders the real error message, not a friendly fiction", () => {
    const reset = jest.fn();
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);

    render(<SegmentError error={new Error("boom: the real render failure")} reset={reset} />);

    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByTestId("error-message")).toHaveTextContent("boom: the real render failure");
    // The failure is also reported once to the console — never swallowed.
    expect(consoleError).toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("calls reset() when the owner retries", () => {
    const reset = jest.fn();
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);

    render(<SegmentError error={new Error("boom")} reset={reset} />);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(reset).toHaveBeenCalledTimes(1);
    consoleError.mockRestore();
  });

  it("says so honestly when no message was attached", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);

    render(<SegmentError error={new Error("")} reset={jest.fn()} />);

    expect(screen.getByTestId("error-message")).toHaveTextContent(
      "Unknown error (no message was attached)",
    );
    consoleError.mockRestore();
  });

  it("shows the digest when the framework attached one", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const error = Object.assign(new Error("boom"), { digest: "digest-abc123" });

    render(<SegmentError error={error} reset={jest.fn()} />);

    expect(screen.getByText(/digest-abc123/)).toBeInTheDocument();
    consoleError.mockRestore();
  });
});

describe("app/error.tsx — X-Request-ID on the failure (T15 / fix-5 §1)", () => {
  it("names the id the API attached to the failed request", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const error = Object.assign(new Error("boom"), { requestId: "req-abc123" });

    render(<SegmentError error={error} reset={jest.fn()} />);

    expect(screen.getByTestId("request-id")).toHaveTextContent("req-abc123");
    // The advice line still says what to do — now with the id named in it.
    expect(screen.getByText(/backend log for this request/)).toBeInTheDocument();
    consoleError.mockRestore();
  });

  it("a BLANK id keeps the generic advice and renders no id line (guard)", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    const error = Object.assign(new Error("boom"), { requestId: "   " });

    render(<SegmentError error={error} reset={jest.fn()} />);

    expect(screen.queryByTestId("request-id")).toBeNull();
    expect(screen.getByText(/backend log for this request \(X-Request-ID\)/)).toBeInTheDocument();
    consoleError.mockRestore();
  });

  it("an error without an id keeps the generic advice", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);

    render(<SegmentError error={new Error("boom")} reset={jest.fn()} />);

    expect(screen.queryByTestId("request-id")).toBeNull();
    consoleError.mockRestore();
  });
});

describe("app/global-error.tsx (root boundary)", () => {
  it("renders a self-contained surface with its own html/body and calls reset", () => {
    const reset = jest.fn();

    render(<GlobalError error={new Error("root layout exploded")} reset={reset} />);

    expect(screen.getByTestId("global-error-message")).toHaveTextContent("root layout exploded");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("carries no dependency that could be the thing that broke", () => {
    // The inline styles are part of the contract: when the root layout fails, the global
    // stylesheet may not have loaded, so the surface must not rely on it.
    const { container } = render(<GlobalError error={new Error("x")} reset={jest.fn()} />);

    const body = container.querySelector("body");
    expect(body).not.toBeNull();
    expect(body?.getAttribute("style")).toContain("background");
  });
});
