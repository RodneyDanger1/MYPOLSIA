"use client";

import { useState } from "react";
import type { FormEvent } from "react";

import { Button } from "@/components/ui";
import {
  DEFAULT_TEMPLATE,
  TEMPLATE_PRESETS,
  validateCustomDomain,
  type SiteDraft,
} from "@/components/sites/types";

/**
 * The create form for `/sites` — the only way a site is born.
 *
 * It owns its boxes and nothing else: the PAGE calls the real `POST /api/v1/sites` and the page
 * renders the server's refusal, so this component never invents a success.  On a successful
 * create (`onCreate` resolves `true`) the boxes clear; on a refusal they keep exactly what the
 * operator typed so the fix is an edit, not a retype.
 *
 * Wave V26 adds two controls:
 *   * the template picker — exactly the three server presets (`classic`/`modern`/`bold`).  The
 *     DEFAULT (`classic`) is omitted from the request so the server's own default applies; choosing
 *     another preset sends its id verbatim.
 *   * the custom-domain field — a bare hostname only, validated here with the same structural rules
 *     the backend enforces, and labelled "recorded, not verified": Polsia stores the name, it never
 *     resolves DNS and never claims the domain is live.
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
  const [template, setTemplate] = useState<string>(DEFAULT_TEMPLATE);
  const [domain, setDomain] = useState("");
  const [domainError, setDomainError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // The inline check runs FIRST: a value like `http://x/y` is refused here with a message that
    // names the shape, instead of a round trip the server would answer with the same refusal.
    const problem = validateCustomDomain(domain);
    if (problem !== null) {
      setDomainError(problem);
      return;
    }
    setDomainError(null);
    const ok = await onCreate({
      title: title.trim(),
      tagline: tagline.trim(),
      template: template === DEFAULT_TEMPLATE ? "" : template,
      custom_domain: domain.trim(),
    });
    if (ok) {
      setTitle("");
      setTagline("");
      setTemplate(DEFAULT_TEMPLATE);
      setDomain("");
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

      <fieldset className="border border-line px-3 py-2">
        <legend className="ui-label text-ink-muted px-1">Template</legend>
        <div className="grid gap-2 sm:grid-cols-3">
          {TEMPLATE_PRESETS.map((preset) => (
            <div key={preset.id} className="flex items-start gap-2">
              <input
                id={`site-template-${preset.id}`}
                type="radio"
                name="site-template"
                value={preset.id}
                checked={template === preset.id}
                onChange={() => setTemplate(preset.id)}
                className="mt-1"
              />
              <div>
                <label
                  htmlFor={`site-template-${preset.id}`}
                  className="text-sm font-semibold text-ink"
                >
                  {preset.label}
                </label>
                <p className="text-xs text-ink-muted">{preset.note}</p>
              </div>
            </div>
          ))}
        </div>
      </fieldset>

      <div>
        <label className="ui-label text-ink-muted" htmlFor="sites-domain">
          Custom domain (optional)
        </label>
        <input
          id="sites-domain"
          className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
          value={domain}
          onChange={(event) => setDomain(event.target.value)}
          placeholder="example.com"
          autoComplete="off"
          spellCheck={false}
          aria-describedby="sites-domain-hint sites-domain-error"
          aria-invalid={domainError !== null}
        />
        <p id="sites-domain-hint" className="text-xs text-ink-muted">
          Recorded, not verified — Polsia stores the hostname; it does not check DNS or point the
          domain at this site.
        </p>
        {domainError !== null ? (
          <p
            role="alert"
            id="sites-domain-error"
            data-testid="sites-domain-error"
            className="text-xs text-ink"
          >
            {domainError}
          </p>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" variant="glossy" disabled={busy}>
          {busy ? "Creating…" : "Create site"}
        </Button>
        <p className="text-xs text-ink-muted">
          The page is built from your company profile and published at{" "}
          <span className="ui-label">/sites/&lt;slug&gt;/</span> — index, about and contact, plus a
          sitemap. An existing slug is refused, never overwritten.
        </p>
      </div>
    </form>
  );
}
