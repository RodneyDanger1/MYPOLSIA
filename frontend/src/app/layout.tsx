import type { Metadata } from "next";

import "./globals.css";
import { Topbar } from "@/components/layout/Topbar";
import { ActivityStrip } from "@/components/shell/ActivityStrip";
import { CommandPalette } from "@/components/shell/CommandPalette";
import { SandboxBanner } from "@/components/system/SandboxBanner";

export const metadata: Metadata = {
  title: "Polsia — AI Business Agent",
  description: "Autonomous AI platform running your company 24/7",
};

/**
 * App shell.
 *
 * Matches the live product's shape: the light canvas holds a header control row
 * (`Polsia · [🔔] [EDIT] [MENU ▾]`) and the page content below it — there is NO
 * persistent navigation rail (agent1-live-chrome/DOSSIER.md cross-cutting A;
 * aid3 MATCH-VS-BEAT C2). Navigation lives in the MENU dropdown.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <a
          href="#content"
          className="ui-label sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:border focus:border-line-strong focus:bg-surface focus:px-3 focus:py-2 focus:text-ink"
        >
          Skip to content
        </a>
        <Topbar />
        <ActivityStrip />
        {/* V11-W6: app-wide sandbox-mode banner. Reads GET /ops/sandbox once; renders only when the
            flag is true, and renders NOTHING on false or a failed read (silence, so a working app
            never looks broken). */}
        <SandboxBanner />
        {/* V8b-D1 (parent mount): the Ctrl+K palette. Renders nothing until opened. */}
        <CommandPalette />
        <main id="content" tabIndex={-1} className="mx-auto w-full max-w-[1600px] px-4 py-6">
          {children}
        </main>
      </body>
    </html>
  );
}
