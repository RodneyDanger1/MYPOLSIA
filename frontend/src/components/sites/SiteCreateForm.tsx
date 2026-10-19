"use client";

import { useState } from "react";
import type { FormEvent } from "react";

import { Button } from "@/components/ui";
import type { SiteDraft } from "@/components/sites/types";

/**
 * The create form for `/sites` — the only way a site is born.
 *
 * It owns its two boxes and nothing else: the PAGE calls the real `POST /api/v1/sites` and the
 * page renders the server's refusal, so this component never invents a success.  On a successful
 * create (`onCreate` resolves `true`) the boxes clear; on a refusal they keep exactly what the
 * operator typed so the fix is an edit, not a retype.
 *
 * Blank boxes are OMITTED from the body rather than sent as `""`: the backend's documented rule is
 * "title defaults to the company name", and an empty string is not that request.
 */
export function SiteCreateForm({
  busy,
  onCreate,
}: {
  busy: boolean;
  onCreate: (draft: SiteDraft) => Promise<boolean>;
}) {
  const [title, setTitle] = useState("");
  const [tagline, setTagline] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const ok = await onCreate({ title: title.trim(), tagline: tagline.trim() });
    if (ok) {
      setTitle("");
      setTagline("");
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3" data-testid="sites-create-form">
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className="ui-label text-ink-muted" htmlFor="sites-title">
            Title
          </label>
          <input
            id="sites-title"
            className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Leave blank to use the company name"
            autoComplete="off"
          />
        </div>
        <div>
          <label className="ui-label text-ink-muted" htmlFor="sites-tagline">
            Tagline
          </label>
          <input
            id="sites-tagline"
            className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={tagline}
            onChange={(event) => setTagline(event.target.value)}
            placeholder="Leave blank to use the company profile"
            autoComplete="off"
          />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" variant="glossy" disabled={busy}>
          {busy ? "Creating…" : "Create site"}
        </Button>
        <p className="text-xs text-ink-muted">
          The page is built from your company profile and published at{" "}
          <span className="ui-label">/sites/&lt;slug&gt;/</span>. An existing slug is refused, never
          overwritten.
        </p>
      </div>
    </form>
  );
}
