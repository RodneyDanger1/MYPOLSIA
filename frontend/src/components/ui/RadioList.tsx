"use client";

import { useRef } from "react";

import { cn } from "@/components/ui/cn";

/**
 * The captured frequency-picker grammar (wave-u1 spec `sheets-and-composer.md` §6): a
 * radio list of `label + note` rows (`DAILY — 30 TASKS/MO`), rendered inside the existing
 * popover chrome (`.panel-pop`) by its consumer — this primitive brings NO panel, chrome
 * or outer margins of its own, only the row hairlines in `--line`.
 *
 * WAI-ARIA radiogroup pattern: `role="radiogroup"` with the `name` as its accessible
 * name; every option is a `role="radio"` element carrying `aria-checked` and rotating
 * `tabIndex` (the checked option — or the first when the value is null — is the single
 * tab stop). ArrowDown/ArrowUp move the selection (wrapping) and focus; Space/Enter
 * select the focused option; clicking an option selects it via `onChange(value)`.
 * Recipe: `globals.css` `.radio-*`.
 */
export type RadioOption<T extends string> = {
  value: T;
  label: string;
  /** Right-aligned qualifier in the mono-caps voice (live: `30 TASKS/MO`). */
  note?: string;
};

export type RadioListProps<T extends string> = {
  options: RadioOption<T>[];
  value: T | null;
  onChange: (v: T) => void;
  /** Group name — the radiogroup's accessible name and each option's group. */
  name: string;
};

export function RadioList<T extends string>({ options, value, onChange, name }: RadioListProps<T>) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const selectedIndex = options.findIndex((option) => option.value === value);
  const tabStop = selectedIndex >= 0 ? selectedIndex : 0;

  const moveTo = (index: number) => {
    const next = options[index];
    if (!next) return;
    onChange(next.value);
    refs.current[index]?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (options.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      moveTo((index + 1) % options.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      moveTo((index - 1 + options.length) % options.length);
    } else if (event.key === " " || event.key === "Enter") {
      event.preventDefault();
      onChange(options[index].value);
    }
  };

  return (
    <div role="radiogroup" aria-label={name} className="radio-list">
      {options.map((option, index) => {
        const checked = option.value === value;
        return (
          <button
            key={option.value}
            ref={(element) => {
              refs.current[index] = element;
            }}
            type="button"
            role="radio"
            name={name}
            aria-checked={checked}
            tabIndex={index === tabStop ? 0 : -1}
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => onKeyDown(event, index)}
            className={cn("radio-item", checked && "radio-item-checked")}
          >
            <span className="radio-item-marker" aria-hidden="true" />
            <span className="ui-label radio-item-label">{option.label}</span>
            {option.note !== undefined ? (
              <span className="ui-label radio-item-note">{option.note}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
