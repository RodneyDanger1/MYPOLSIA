"use client";

import type { CSSProperties } from "react";

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
 *
 * Wave U4 restyles it to the live design language — light canvas, white panel, serif voice,
 * mono ALL-CAPS labels, black glossy primary + bevel secondary — as INLINE approximations
 * of the `globals.css` recipes (`.btn`, `.btn-bevel`, `.btn-glossy` and the token palette),
 * because the class layer is exactly what this file may not have. Every value here is
 * copied from the token definitions in `src/app/globals.css`.
 */

// Token values, inlined (the stylesheet may not have loaded — see the docblock above).
const INK = "#111111";
const INK_MUTED = "#5F5F5F";
const LINE = "#E0E0E0";
const LINE_STRONG = "#6B6B6B";
const ALERT = "#9B1C1C";
const MONO =
  "ui-monospace, 'Cascadia Mono', Consolas, 'SF Mono', Menlo, 'DejaVu Sans Mono', monospace";
const SERIF = "Georgia, 'Times New Roman', Times, serif";

/** The `.ui-label` recipe: mono, ALL CAPS, letter-spaced micro-label. */
const LABEL_STYLE: CSSProperties = {
  fontFamily: MONO,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
  fontSize: "0.6875rem",
  lineHeight: 1.25,
  margin: 0,
};

/** The `.btn` recipe base. */
const BUTTON_BASE: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: "3px",
  padding: "0.3125rem 0.625rem",
  fontFamily: MONO,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
  fontSize: "0.6875rem",
  lineHeight: 1.15,
  cursor: "pointer",
};

/** The `.btn-glossy` recipe: the black glossy CTA. */
const GLOSSY_BUTTON: CSSProperties = {
  ...BUTTON_BASE,
  color: "#ffffff",
  border: "1px solid #000000",
  backgroundImage: "linear-gradient(180deg, #5a5a5a 0%, #2b2b2b 48%, #0d0d0d 52%, #000000 100%)",
};

/** The `.btn-bevel` recipe: the light-gray beveled button. */
const BEVEL_BUTTON: CSSProperties = {
  ...BUTTON_BASE,
  color: INK,
  border: `1px solid ${LINE_STRONG}`,
  backgroundImage: "linear-gradient(180deg, #ffffff 0%, #f2f2f2 45%, #dcdcdc 100%)",
};

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
          background: "#F0F0F0",
          color: INK,
          fontFamily: SERIF,
        }}
      >
        <div style={{ maxWidth: "42rem", margin: "0 auto", padding: "3rem 1.5rem" }}>
          <div
            style={{
              background: "#FFFFFF",
              border: `1px solid ${LINE}`,
              borderRadius: "3px",
              padding: "1.25rem",
            }}
          >
            <p style={{ ...LABEL_STYLE, color: INK_MUTED, margin: "0 0 0.25rem" }}>Root failure</p>
            <h1 style={{ fontSize: "1.5rem", fontWeight: 700, margin: "0 0 0.75rem" }}>
              Polsia failed to start
            </h1>
            <p style={{ color: INK_MUTED, fontSize: "0.875rem", margin: "0 0 1rem" }}>
              The root layout crashed, so nothing could be rendered inside it. This page is the
              app&apos;s own error surface — not a blank screen — and the message below is the real
              one the browser received.
            </p>
            <div
              role="alert"
              style={{
                border: `1px solid ${ALERT}`,
                background: "#FFFFFF",
                padding: "0.75rem",
                margin: "0 0 1rem",
              }}
            >
              <p style={{ ...LABEL_STYLE, color: ALERT, margin: "0 0 0.25rem" }}>Error detail</p>
              <pre
                data-testid="global-error-message"
                style={{
                  margin: 0,
                  fontFamily: MONO,
                  fontSize: "0.75rem",
                  color: INK,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {error.message || "Unknown error (no message was attached)"}
              </pre>
              {error.digest ? (
                <p style={{ ...LABEL_STYLE, color: INK_MUTED, margin: "0.5rem 0 0" }}>
                  Digest: {error.digest}
                </p>
              ) : null}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem" }}>
              <button onClick={() => reset()} style={GLOSSY_BUTTON}>
                Try again
              </button>
              <button onClick={() => window.location.reload()} style={BEVEL_BUTTON}>
                Reload the page
              </button>
            </div>
          </div>
        </div>
      </body>
    </html>
  );
}
