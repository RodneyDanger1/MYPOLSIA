import type { Metadata } from "next";

import "./globals.css";
import { Topbar } from "@/components/layout/Topbar";

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
        <main id="content" tabIndex={-1} className="mx-auto w-full max-w-[1600px] px-4 py-6">
          {children}
        </main>
      </body>
    </html>
  );
}
