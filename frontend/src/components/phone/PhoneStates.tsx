/**
 * The phone family's one honest "the store is not here" state — shared by the dashboard card and
 * the `/phone` page so both surfaces say the exact same thing.
 *
 * This is a CAPABILITY absence, not a fault: the deployment simply has no phone-link store mounted
 * (the named `E_PHONE_UNAVAILABLE` refusal, or a summary answering `store_status` != "ok"). The
 * presentation is deliberately neutral (the quiet inset surface, mono-caps label), the same voice
 * `WebsiteCard` uses for its refuse-by-name notes — red is reserved for real faults. Nothing is
 * ever rendered beside this block: no rows, no counts, no empty-list copy.
 */
export function PhoneUnavailable({ testId, detail }: { testId: string; detail: string }) {
  return (
    <div role="note" data-testid={testId} className="border border-line bg-surface-sunk p-3">
      <p className="ui-label text-ink-muted">PHONE LINK UNAVAILABLE</p>
      <p className="mt-1 text-sm text-ink">{detail}</p>
    </div>
  );
}
