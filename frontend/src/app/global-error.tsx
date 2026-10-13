"use client";

/**
 * Root error boundary (app router — L5-02).
 *
 * `global-error.tsx` catches failures in the ROOT layout itself, where the normal
 * `error.tsx` cannot render — and in that situation the app's global stylesheet may not
 * even have loaded.  So this surface carries its OWN `<html>`/`<body>` tags and inline
 * styles (Next.js requires the markup; Tailwind classes are not guaranteed to be applied
 * when the layout that loads them is the thing that crashed).
 *
 * Honest by construction: the real `error.message` is rendered, not a friendly fiction,
 * and both buttons do real work (`reset()` re-renders; reload re-fetches the document).
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          background: "#020617",
          color: "#e2e8f0",
          fontFamily:
            "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
        }}
      >
        <div style={{ maxWidth: "42rem", margin: "0 auto", padding: "3rem 1.5rem" }}>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 700, margin: "0 0 0.75rem" }}>
            Polsia failed to start
          </h1>
          <p style={{ color: "#94a3b8", fontSize: "0.875rem", margin: "0 0 1rem" }}>
            The root layout crashed, so nothing could be rendered inside it. This page is the
            app&apos;s own error surface — not a blank screen — and the message below is the real
            one the browser received.
          </p>
          <pre
            data-testid="global-error-message"
            style={{
              background: "#0f172a",
              border: "1px solid #1e293b",
              borderRadius: "0.5rem",
              padding: "1rem",
              fontSize: "0.75rem",
              color: "#fca5a5",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              margin: "0 0 1rem",
            }}
          >
            {error.message || "Unknown error (no message was attached)"}
          </pre>
          {error.digest ? (
            <p style={{ color: "#64748b", fontSize: "0.75rem", margin: "0 0 1rem" }}>
              Digest: {error.digest}
            </p>
          ) : null}
          <div style={{ display: "flex", gap: "0.75rem" }}>
            <button
              onClick={() => reset()}
              style={{
                padding: "0.5rem 1rem",
                background: "#4f46e5",
                color: "#ffffff",
                border: "none",
                borderRadius: "0.375rem",
                fontSize: "0.875rem",
                cursor: "pointer",
              }}
            >
              Try again
            </button>
            <button
              onClick={() => window.location.reload()}
              style={{
                padding: "0.5rem 1rem",
                background: "#334155",
                color: "#ffffff",
                border: "none",
                borderRadius: "0.375rem",
                fontSize: "0.875rem",
                cursor: "pointer",
              }}
            >
              Reload the page
            </button>
          </div>
        </div>
      </body>
    </html>
  );
}
