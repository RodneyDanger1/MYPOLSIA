"use client";

import { useEffect } from "react";

import { Button } from "@/components/ui";

/**
 * Segment-level error boundary (app router — L5-02).
 *
 * Before this file existed the app had no `error.tsx` anywhere, so any unhandled render
 * error in a route segment produced the framework's blank failure: the owner saw an empty
 * screen with no explanation.  An error boundary in the app router must be a client
 * component; this one renders an honest surface — the real `error.message`, an optional
 * digest for log correlation, and buttons that actually do something (`reset()` re-renders
 * the segment, reload re-fetches the document).
 *
 * Wave U4 moved the surface onto the live design language (white panel on the canvas, serif
 * heading, mono-caps labels, the ui `Button` primitive) — behaviour unchanged. The boundary
 * is still dependency-light on purpose: the primitive it imports (`@/components/ui`) is a
 * purely local presentational wrapper — React plus `cn` over the `globals.css` recipes — with
 * no data, state or network dependencies, so a failure in the data layer cannot prevent this
 * boundary from rendering.  No key material is involved anywhere — nothing is sent over the
 * network here.  The X-Request-ID is read off the error the boundary RECEIVED (duck-typed
 * below): an app-router error boundary is a Client Component, and Next exposes no request
 * headers to one (nextjs docs, file-conventions/error: "'use client' — Error boundaries must
 * be Client Components").
 */

/** The server's `X-Request-ID` for the failed call, when the error carries one (fix-5 §1 / T15c).
 *
 *  Duck-typed on purpose: the id arrives attached to a client-thrown `ApiError.requestId` (see
 *  lib/api.ts), and this boundary must not import anything to read it.  A blank/whitespace-only
 *  value is treated as ABSENT — the page must never render an empty id shell (the same
 *  blank-field guard api.ts applies to the X-API-Key header).
 */
function requestIdOf(error: Error): string | null {
  const carried = (error as Error & { requestId?: unknown }).requestId;
  return typeof carried === "string" && carried.trim() !== "" ? carried.trim() : null;
}

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // The real error stays visible (rendered below) and is reported once to the console.
    console.error("Unhandled render error:", error);
  }, [error]);

  const requestId = requestIdOf(error);

  return (
    <div className="max-w-2xl space-y-4">
      <div className="border-b border-line pb-1">
        <p className="ui-label text-ink-muted">Unexpected failure</p>
        <h1 className="text-3xl font-bold leading-tight text-ink">Something went wrong</h1>
      </div>
      <p className="text-sm text-ink">
        This screen failed to render. The app did not hide the failure behind an empty page —
        the details below are what it actually received.
      </p>
      <div role="alert" className="border border-alert bg-surface p-3">
        <p className="ui-label text-alert">Error detail</p>
        <pre
          data-testid="error-message"
          className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-ink"
        >
          {error.message || "Unknown error (no message was attached)"}
        </pre>
        {error.digest ? (
          <p className="ui-label mt-2 text-ink-muted">Digest: {error.digest}</p>
        ) : null}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="glossy" onClick={() => reset()}>
          Try again
        </Button>
        <Button variant="bevel" onClick={() => window.location.reload()}>
          Reload the page
        </Button>
      </div>
      {requestId ? (
        // The id NAMES the request instead of telling the user to hunt for it (fix-5 §1).
        <p className="ui-label text-ink-muted">
          If it keeps happening, the backend log for this request (X-Request-ID{" "}
          <span data-testid="request-id" className="font-mono text-ink">
            {requestId}
          </span>
          ) has the server side.
        </p>
      ) : (
        <p className="ui-label text-ink-muted">
          If it keeps happening, the backend log for this request (X-Request-ID) has the server side.
        </p>
      )}
    </div>
  );
}
