import Link from "next/link";

/**
 * The app's 404 surface (`app/not-found.tsx`).
 *
 * Next renders this inside `app/layout.tsx` for any address no route matches — and also for a
 * route that calls `notFound()`. It is a server segment on purpose: it holds no state, no read
 * and no client hook, so it cannot fail for the same reason a page did. The shell (topbar, menu,
 * skip link) wraps it automatically, which is why it emits no document element of its own.
 *
 * Honesty:
 *   - the heading states the HTTP fact; it does not apologise or pretend the page "moved";
 *   - the requested address is NOT echoed. Next hands this surface no path, and a fabricated one
 *     would be the worst kind of 404 copy — a lie that looks helpful;
 *   - the destinations below are the menu's real, most-used work surfaces, so the owner can carry
 *     on in one click instead of retyping an address.
 *
 * The visual language is the app's own: mono-caps label over a serif heading, hairline rules and
 * menu-style rows — the same voice as `error.tsx`, not a generic centred card.
 */

/** The menu's most-used work surfaces. Paths and labels mirror `components/layout/nav-items.ts`. */
const SURFACES = [
  { href: "/tasks", label: "Tasks", note: "queue" },
  { href: "/agents", label: "Agents", note: "roster" },
  { href: "/social", label: "Social", note: "posts" },
] as const;

export default function NotFound() {
  return (
    <div data-testid="not-found" className="max-w-2xl space-y-5">
      <div className="border-b border-line pb-1">
        <p className="ui-label text-ink-muted">404 · No such route</p>
        <h1 className="text-3xl font-bold leading-tight text-ink">This page does not exist</h1>
      </div>

      <div className="space-y-2 text-sm text-ink">
        <p>
          The address you opened is not one this build serves. Nothing was redirected and no page is
          hidden behind an empty state — there is simply no route at that address.
        </p>
        <p className="text-ink-muted">
          The address is not repeated here: the 404 surface is rendered without it, and inventing one
          would be fiction. Use one of the real routes below.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Link data-testid="not-found-home" href="/dashboard" className="btn btn-glossy">
          Back to the dashboard
        </Link>
        <span className="ui-label text-ink-muted">or jump to</span>
      </div>

      <nav aria-label="Common surfaces" className="border-y border-line">
        <ul className="divide-y divide-line">
          {SURFACES.map(({ href, label, note }) => (
            <li key={href}>
              <Link
                href={href}
                className="ui-label flex items-center gap-2 py-2 pl-2 pr-1 text-ink hover:bg-surface-strong"
              >
                <span aria-hidden="true" className="w-3 shrink-0 text-center text-[10px]">
                  ▸
                </span>
                {label}
                <span className="ml-auto text-ink-muted">{note}</span>
              </Link>
            </li>
          ))}
        </ul>
      </nav>

      <p className="ui-label text-ink-muted">
        The menu in the top bar lists every surface this build serves.
      </p>
    </div>
  );
}
