"use client";

import { useEffect } from "react";

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
 * Dependency-free on purpose: if a dependency is what broke, the boundary must not need it
 * to render.  No key material is involved anywhere — nothing is sent over the network here.
 */
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

  return (
    <div className="p-6 max-w-2xl space-y-4">
      <h1 className="text-2xl font-bold text-white">Something went wrong</h1>
      <p className="text-gray-300 text-sm">
        This screen failed to render. The app did not hide the failure behind an empty page —
        the details below are what it actually received.
      </p>
      <pre
        data-testid="error-message"
        className="bg-gray-800 border border-gray-700 rounded-lg p-4 text-xs text-red-300 whitespace-pre-wrap break-words"
      >
        {error.message || "Unknown error (no message was attached)"}
      </pre>
      {error.digest && <p className="text-gray-500 text-xs">Digest: {error.digest}</p>}
      <div className="flex flex-wrap gap-3">
        <button
          onClick={() => reset()}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-md text-sm"
        >
          Try again
        </button>
        <button
          onClick={() => window.location.reload()}
          className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-md text-sm"
        >
          Reload the page
        </button>
      </div>
      <p className="text-gray-500 text-xs">
        If it keeps happening, the backend log for this request (X-Request-ID) has the server side.
      </p>
    </div>
  );
}
