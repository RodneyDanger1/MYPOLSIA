/**
 * `/tasks/[id]` id parsing — extracted from the page module by the parent at V11 unit-2 settle.
 *
 * Why: a Next App Router page may only export its page fields (default / config / metadata /
 * generateStaticParams / …). The named `parseTaskId` export passed tsc and jest but failed
 * `next build`'s checkFields ("Property 'parseTaskId' is incompatible with index signature"),
 * so the production build refused while every green check stayed green. The contract is
 * unchanged — same function, same behavior, one module over.
 */

/** Same contract as `/tasks?task=N`: digits only, a positive safe integer; else no request. */
export function parseTaskId(raw: string | null | undefined): number | null {
  if (raw === null || raw === undefined || !/^\d+$/.test(raw)) return null;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}
