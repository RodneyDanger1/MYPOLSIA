"use client";

import { Button } from "@/components/ui";

export type ComposerProps = {
  value: string;
  onChange: (next: string) => void;
  onSubmit: () => void;
  /** A POST /api/v1/chat is in flight — SEND is disabled and says so. */
  sending: boolean;
};

/**
 * The message composer: the rail's one text input and its ONE primary affordance (the glossy
 * `SEND` button — the same species the live product reserves for `SEND`, per the UI primitives'
 * doc). Blank input disables SEND rather than sending whitespace.
 *
 * Keyboard contract: plain Enter submits; Shift+Enter keeps the browser's newline. The Enter
 * that commits an IME candidate is left alone (`isComposing`), or every CJK composition would
 * fire a premature send.
 */
export function Composer({ value, onChange, onSubmit, sending }: ComposerProps) {
  const canSend = !sending && value.trim() !== "";

  return (
    <div className="border-t border-[#E3E3E3] px-3 py-2.5">
      <div className="flex items-end gap-2">
        <textarea
          aria-label="Message"
          rows={2}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              // Plain Enter never inserts a newline here — it is the send key (Shift+Enter is
              // the one that keeps the browser's line break).
              e.preventDefault();
              if (canSend) onSubmit();
            }
          }}
          placeholder="Ask about status, or request a task…"
          className="min-h-[49px] w-full resize-none border border-line-strong bg-surface px-3 py-2 text-sm text-ink"
        />
        <Button variant="glossy" size="sm" onClick={onSubmit} disabled={!canSend}>
          {sending ? "SENDING…" : "SEND"}
        </Button>
      </div>
      <p className="mt-1.5 font-serif text-[11px] text-[#6e6e6e]">
        Enter sends · Shift+Enter adds a line. A requested task is created only after you confirm
        it.
      </p>
    </div>
  );
}
