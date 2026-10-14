import type { Config } from "tailwindcss";

/**
 * Tailwind surface for the live-language token layer in `src/app/globals.css`.
 *
 * Role names (canvas / band / surface / ink / link / accent / alert) are the ones
 * later waves consume; each maps to an HSL triplet CSS variable so opacity
 * modifiers work (`bg-band/80`). Nothing here invents a colour: every hex is
 * measured in agent1-live-chrome/DOSSIER.md "Global palette" / agent2-code
 * CAPTURES.md cap-01, and the accessible deviations are documented at the token.
 */
export default {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        /** #F0F0F0 page canvas */
        canvas: "hsl(var(--canvas) / <alpha-value>)",
        /** White cards / panels / dropdowns, plus their inset shades */
        surface: {
          DEFAULT: "hsl(var(--surface) / <alpha-value>)",
          sunk: "hsl(var(--surface-sunk) / <alpha-value>)",
          strong: "hsl(var(--surface-strong) / <alpha-value>)",
        },
        /** Pure-black chrome bands (live-log strip) and their text colour */
        band: {
          DEFAULT: "hsl(var(--band) / <alpha-value>)",
          ink: "hsl(var(--band-ink) / <alpha-value>)",
        },
        /** Near-black body/heading text and its muted companion */
        ink: {
          DEFAULT: "hsl(var(--ink) / <alpha-value>)",
          muted: "hsl(var(--ink-muted) / <alpha-value>)",
        },
        /** Hairlines and control borders */
        line: {
          DEFAULT: "hsl(var(--line) / <alpha-value>)",
          strong: "hsl(var(--line-strong) / <alpha-value>)",
        },
        /** Blue links */
        link: "hsl(var(--link) / <alpha-value>)",
        /** Orange accents — fill/dot/outline plus the label colour that sits on it */
        accent: {
          DEFAULT: "hsl(var(--accent) / <alpha-value>)",
          soft: "hsl(var(--accent-soft) / <alpha-value>)",
          ink: "hsl(var(--accent-ink) / <alpha-value>)",
        },
        /** Dark-red NEW marker / destructive, plus its label colour */
        alert: {
          DEFAULT: "hsl(var(--alert) / <alpha-value>)",
          ink: "hsl(var(--alert-ink) / <alpha-value>)",
        },
        /** Keyboard focus ring (always drawn as an outline) */
        focus: "hsl(var(--focus) / <alpha-value>)",
      },
      fontFamily: {
        /** Live display/body voice: serif. System stack only — no webfonts. */
        serif: ["var(--font-serif)"],
        /** Live label/button/chip/timestamp voice: monospace, ALL CAPS. */
        mono: ["var(--font-mono)"],
      },
    },
  },
  plugins: [],
} satisfies Config;
