import { Card } from "@/components/ui";

/**
 * Middle column, bottom (under the Daily Report): the live product's `Media` slot, built to the
 * ui-gap §8 honest-minimum decision.
 *
 * The body is the live's own empty line. The live's `IMAGE` / `VIDEO` buttons and its dashed
 * `+ NEW CAPABILITY` tile are deliberately NOT drawn: this deployment ships no image/video
 * generator, no media storage and no endpoint behind either control, so buttons would be dead
 * affordances — the exact defect this campaign exists to remove (ui-gap §8: "The honest minimum:
 * a `Card title="Media"` … with NO dead buttons"). The absence IS the decision.
 *
 * It has no reads, no props and no state, so it renders identically in every state — there is
 * nothing here that can go stale, load, or fail.
 */
export function MediaCard() {
  return (
    <Card title="Media">
      <p className="text-sm text-ink-muted">
        No media yet — this deployment ships no image/video generator yet.
      </p>
    </Card>
  );
}
