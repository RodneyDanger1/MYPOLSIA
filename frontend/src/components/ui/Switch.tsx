"use client";

/**
 * The live product's ON/OFF switch — NOT a pill (measured: wave-u1 spec
 * `sheets-and-composer.md` §5): a two-segment squared control (~92x26 px, ~2 px
 * radius, 1px dark border + a divider between the segments). The ON segment is
 * the existing `.chip-accent` pair (fill `--accent`, label `--accent-ink`); the
 * OFF segment stays in the existing neutral family (`--surface-strong` fill,
 * `--ink-muted` label) — no new colours. The side matching `checked` keeps full
 * strength and the pressed inset; the other side steps down within the same
 * tokens, so the current state is visible without inventing anything.
 *
 * State is exposed as `role="switch"` + `aria-checked`; the `label` prop is the
 * accessible name. Clicking anywhere on the control toggles via `onChange(next)`;
 * `disabled` carries the real `disabled` attribute and never fires.
 * Recipe: `globals.css` `.seg-switch*`.
 */
export type SwitchProps = {
  checked: boolean;
  onChange: (next: boolean) => void;
  /** Accessible name (the control's visible text is the ON/OFF segment pair). */
  label: string;
  disabled?: boolean;
  id?: string;
};

export function Switch({ checked, onChange, label, disabled = false, id }: SwitchProps) {
  return (
    <button
      type="button"
      id={id}
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="seg-switch"
    >
      <span aria-hidden="true" className="seg-switch-seg seg-switch-on ui-label">
        ON
      </span>
      <span aria-hidden="true" className="seg-switch-seg seg-switch-off ui-label">
        OFF
      </span>
    </button>
  );
}
