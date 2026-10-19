"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { API_KEY, ApiError, api } from "@/lib/api";
import { Button, DataTable, SectionHeader } from "@/components/ui";

/**
 * Settings → Data (V11/W4) — the owner's data portability surface, wired to the REAL backend
 * (`backend/app/api/v1/data.py`):
 *
 *   GET    /api/v1/data/export        -> 200 application/zip (MANIFEST.json + 8 members)
 *   DELETE /api/v1/data?dry_run=true  -> would-delete counts; never deletes, never consumes a token
 *   POST   /api/v1/approvals          -> mint ONE confirm token for the wipe action_fingerprint
 *   DELETE /api/v1/data?confirm_token=<token> -> the guarded, single-use wipe
 *
 * The UI performs exactly that sequence and nothing else. Nothing here fires on mount — every
 * request is behind an explicit click, and the wipe additionally requires the dry run's
 * fingerprint, a minted token, and a second explicit confirmation before DELETE leaves the page.
 *
 * WHY raw `fetch` for the export and not `api.get`: lib/api.ts is JSON-only by construction
 * (`apiFetch` ends in `res.json()`), so a ZIP body through it would reject after the response
 * arrived — the identical reason (and the identical mirrored refusal parser) as the shipped
 * `components/usage/ExportButton.tsx` (GET /runs/export). Auth rides the client's single source of
 * truth: `API_KEY` is imported from `@/lib/api`, sent only when non-empty, against the same
 * base-URL convention.
 *
 * The wipe's refusal vocabulary is rendered BY NAME (data.py): E_VALIDATION (malformed / absent
 * token, or every member table already empty — token NOT burned), E_POLICY_DENIED (expired,
 * invalid, or ALREADY-USED single-use token), E_CONFIG (APPROVAL_SECRET unset), E_STORAGE (the
 * consumed-token marker is unreadable/unwritable), E_DB (mid-transaction failure, rolled back).
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost";

/** `DataWipeResponse` (data.py) — dry_run answers `would_delete`, the wipe answers `deleted`. */
type DataWipeDryRun = {
  ok: boolean;
  dry_run: boolean;
  action_fingerprint: string;
  would_delete: Record<string, number> | null;
  total_rows: number;
  kept: string[];
  how_to_execute: string | null;
};
type DataWipeResult = {
  ok: boolean;
  dry_run: boolean;
  action_fingerprint: string;
  deleted: Record<string, number> | null;
  total_rows: number;
  kept: string[];
};
/** `ApprovalIssueResponse` (approvals.py): 201 {token, action_fingerprint, expires_at, ttl_s}. */
type MintedApproval = {
  token: string;
  action_fingerprint: string;
  expires_at: number;
  ttl_s: number;
};

/** The refusal carried by a response body, in the shapes lib/api.ts parses (envelope + detail).
 *  Mirrored, not imported: lib/api.ts does not export the parser (its owner's call — the same
 *  mirror `ExportButton.tsx` carries). */
function refusalOf(body: unknown): { code: string; message: string; howToFix: string } | null {
  if (!body || typeof body !== "object") return null;
  const b = body as { ok?: unknown; error?: unknown; detail?: unknown };
  const candidate =
    b.ok === false && b.error !== null && typeof b.error === "object"
      ? (b.error as Record<string, unknown>)
      : b.detail !== null && typeof b.detail === "object"
        ? (b.detail as Record<string, unknown>)
        : null;
  if (!candidate || typeof candidate.code !== "string") return null;
  return {
    code: candidate.code,
    message: typeof candidate.message === "string" ? candidate.message : candidate.code,
    howToFix: typeof candidate.how_to_fix === "string" ? candidate.how_to_fix : "",
  };
}

/** ApiError carries `.display` (code + message + fix); anything else shows its own message. */
function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

/** The download filename: the server's Content-Disposition when CORS exposes it, else the
 *  server's own name shape printed verbatim (data.py: `polsia-data-export-<date>.zip`). */
function downloadName(header: string | null): string {
  if (header) {
    const match = /filename="?([^";]+)"?/.exec(header);
    if (match) return match[1];
  }
  return "polsia-data-export.zip";
}

function numberOrDash(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("en-US") : "—";
}

/** One per-table count table, null-safe: a wire without counts says so instead of rendering 0. */
function CountTable({ counts }: { counts: Record<string, number> | null }) {
  if (counts === null) {
    return (
      <p className="text-sm text-ink">The server did not report per-table counts for this step.</p>
    );
  }
  const rows = Object.entries(counts);
  return (
    <DataTable
      label="Database tables and their row counts"
      columns={[
        { key: "table", header: "Table", render: (row: [string, unknown]) => row[0] },
        {
          key: "rows",
          header: "Rows",
          render: (row: [string, unknown]) => numberOrDash(row[1]),
        },
      ]}
      rows={rows}
      rowKey={(row) => row[0]}
      empty={<span className="ui-label text-ink-muted">The server reported an empty member set.</span>}
    />
  );
}

export function DataSection() {
  // Export (raw fetch — the ZIP read lib/api.ts cannot do).
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exportDone, setExportDone] = useState<string | null>(null);

  // The guarded wipe, step by step.
  const [dryRun, setDryRun] = useState<DataWipeDryRun | null>(null);
  const [dryRunBusy, setDryRunBusy] = useState(false);
  const [dryRunError, setDryRunError] = useState<string | null>(null);

  const [minted, setMinted] = useState<MintedApproval | null>(null);
  const [mintBusy, setMintBusy] = useState(false);
  const [mintError, setMintError] = useState<string | null>(null);

  const [confirming, setConfirming] = useState(false);
  const [wiping, setWiping] = useState(false);
  const [wipeError, setWipeError] = useState<string | null>(null);
  const [wipeResult, setWipeResult] = useState<DataWipeResult | null>(null);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const onExport = useCallback(async () => {
    setExporting(true);
    setExportError(null);
    setExportDone(null);
    try {
      const res = await fetch(`${API_URL}/api/v1/data/export`, {
        headers: { ...(API_KEY ? { "X-API-Key": API_KEY } : {}) },
      });
      if (!res.ok) {
        const text = await res.text().catch(() => res.statusText);
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = undefined;
        }
        const refusal = refusalOf(parsed);
        throw refusal
          ? new ApiError(res.status, refusal.message, refusal.code, refusal.howToFix, parsed)
          : new ApiError(res.status, text || res.statusText, `E_HTTP_${res.status}`);
      }
      const name = downloadName(res.headers.get("Content-Disposition"));
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      if (!mounted.current) return;
      const declared = res.headers.get("X-Polsia-Row-Count");
      setExportDone(
        declared === null ? `Exported ${name}.` : `Exported ${declared} rows to ${name}.`,
      );
    } catch (e: unknown) {
      if (mounted.current) setExportError(errorText(e));
    } finally {
      if (mounted.current) setExporting(false);
    }
  }, []);

  const onDryRun = useCallback(async () => {
    setDryRunBusy(true);
    setDryRunError(null);
    // A new dry run invalidates any token minted for the previous reading — the counts it
    // authorized may have changed since.
    setMinted(null);
    setConfirming(false);
    try {
      const response = await api.delete<DataWipeDryRun>("/data?dry_run=true");
      if (!mounted.current) return;
      setDryRun(response);
    } catch (e: unknown) {
      if (mounted.current) setDryRunError(errorText(e));
    } finally {
      if (mounted.current) setDryRunBusy(false);
    }
  }, []);

  const onMint = useCallback(async () => {
    if (!dryRun) return;
    setMintBusy(true);
    setMintError(null);
    try {
      const response = await api.post<MintedApproval>("/approvals", {
        action_fingerprint: dryRun.action_fingerprint,
      });
      if (!mounted.current) return;
      setMinted(response);
    } catch (e: unknown) {
      if (mounted.current) setMintError(errorText(e));
    } finally {
      if (mounted.current) setMintBusy(false);
    }
  }, [dryRun]);

  const onWipe = useCallback(async () => {
    if (!minted) return;
    setWiping(true);
    setWipeError(null);
    try {
      const response = await api.delete<DataWipeResult>(
        `/data?confirm_token=${encodeURIComponent(minted.token)}`,
      );
      if (!mounted.current) return;
      setWipeResult(response);
      // The token is single-use: the server burned it (or refused — then the catch kept it).
      // Either way this page will not present it again.
      setMinted(null);
      setDryRun(null);
      setConfirming(false);
    } catch (e: unknown) {
      if (mounted.current) {
        setWipeError(errorText(e));
        setConfirming(false);
      }
    } finally {
      if (mounted.current) setWiping(false);
    }
  }, [minted]);

  const total = dryRun === null ? null : numberOrDash(dryRun.total_rows);
  const emptyMembers = dryRun !== null && dryRun.total_rows === 0;
  const canMint = dryRun !== null && !emptyMembers && !mintBusy;
  const canWipe = minted !== null && !wiping;

  return (
    <section className="card-surface p-4 space-y-4" aria-label="Data export and wipe">
      <SectionHeader kicker="OWNER DATA" title="Export & wipe" />
      <p className="text-sm text-ink">
        The export is one ZIP: <span className="ui-label">MANIFEST.json</span> plus the 8 member
        tables (GET /api/v1/data/export). The wipe below deletes the member rows and provably keeps
        the company config and the DeepSeek key store.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" data-testid="settings-data-export" onClick={onExport} disabled={exporting}>
          {exporting ? "Exporting…" : "Download data export"}
        </Button>
        {exportError !== null ? (
          <span role="alert" data-testid="settings-data-export-error" className="text-sm text-alert">
            Export failed: {exportError}
          </span>
        ) : null}
        {exportDone !== null ? (
          <span role="status" data-testid="settings-data-export-done" className="ui-label text-ink-muted">
            {exportDone}
          </span>
        ) : null}
      </div>

      <div className="space-y-3 border-t border-line pt-4">
        <p className="ui-label text-ink">Guarded wipe</p>
        <p className="ui-label text-ink-muted">
          The backend requires this exact order: dry run → mint a single-use confirm token
          (POST /api/v1/approvals) → confirm the wipe. Nothing here runs automatically, and the
          token can be used once.
        </p>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            data-testid="settings-data-dryrun"
            onClick={onDryRun}
            disabled={dryRunBusy || wiping}
          >
            {dryRunBusy ? "Checking…" : "Dry run (what would be deleted)"}
          </Button>
          <Button size="sm" data-testid="settings-data-mint" onClick={onMint} disabled={!canMint}>
            {mintBusy ? "Minting…" : "Mint confirm token"}
          </Button>
        </div>

        {dryRunError !== null ? (
          <div role="alert" className="border border-alert bg-surface p-3">
            <p className="ui-label text-alert">Dry run refused</p>
            <p className="mt-1 text-sm text-ink">{dryRunError}</p>
          </div>
        ) : null}

        {mintError !== null ? (
          <div role="alert" className="border border-alert bg-surface p-3">
            <p className="ui-label text-alert">Token mint refused</p>
            <p className="mt-1 text-sm text-ink">{mintError}</p>
          </div>
        ) : null}

        {dryRun !== null ? (
          <div className="space-y-2">
            <p className="ui-label text-ink">
              Dry run — an unexecuted delete would remove {total} rows:
            </p>
            <CountTable counts={dryRun.would_delete} />
            <p className="text-sm text-ink">
              Kept regardless: {Array.isArray(dryRun.kept) ? dryRun.kept.join(", ") : "—"}.
            </p>
            <p className="ui-label text-ink-muted">
              action_fingerprint: {dryRun.action_fingerprint}
            </p>
            {dryRun.how_to_execute !== null ? (
              <p className="ui-label text-ink-muted">Server&apos;s own instruction: {dryRun.how_to_execute}</p>
            ) : null}
            {emptyMembers ? (
              <p role="status" className="text-sm text-ink" data-testid="settings-data-empty">
                Every member table is empty — the backend would refuse a wipe here (E_VALIDATION)
                without consuming a token, so the mint and wipe steps are disabled.
              </p>
            ) : null}
          </div>
        ) : null}

        {minted !== null ? (
          <p role="status" data-testid="settings-data-minted" className="text-sm text-ink">
            Confirm token minted — it is single-use. Wipe now, or re-run the dry run to mint a
            fresh one. (ttl {numberOrDash(minted.ttl_s)}s)
          </p>
        ) : null}

        {wipeResult !== null ? (
          <div className="space-y-2" data-testid="settings-data-wiped">
            <p role="status" className="border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink">
              Wipe complete — {numberOrDash(wipeResult.total_rows)} rows deleted. Kept:{" "}
              {Array.isArray(wipeResult.kept) ? wipeResult.kept.join(", ") : "—"}. The confirm token
              is consumed; presenting it again would be refused (E_POLICY_DENIED).
            </p>
            <CountTable counts={wipeResult.deleted} />
            <p className="ui-label text-ink-muted">
              Run a fresh dry run to read the (now empty) member set.
            </p>
          </div>
        ) : null}

        {wipeError !== null ? (
          <div role="alert" data-testid="settings-data-wipe-error" className="border border-alert bg-surface p-3">
            <p className="ui-label text-alert">Wipe refused — nothing was deleted</p>
            <p className="mt-1 text-sm text-ink">{wipeError}</p>
          </div>
        ) : null}

        {canWipe && !confirming ? (
          <Button
            size="sm"
            data-testid="settings-data-wipe"
            onClick={() => setConfirming(true)}
            disabled={wiping}
          >
            Wipe all data…
          </Button>
        ) : null}

        {confirming && minted !== null ? (
          <div
            role="group"
            aria-label="Confirm data wipe"
            data-testid="settings-data-confirm"
            className="space-y-2 border border-alert bg-surface p-3"
          >
            <p className="ui-label text-alert">
              This permanently deletes {total === null ? "the member rows" : `${total} rows`} from
              every member table. The company config and the DeepSeek key store are kept. The
              confirm token is single-use and will be burned by this action.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" data-testid="settings-data-wipe-confirm" onClick={onWipe} disabled={wiping}>
                {wiping ? "Wiping…" : "Confirm wipe — delete now"}
              </Button>
              <Button size="sm" data-testid="settings-data-wipe-cancel" onClick={() => setConfirming(false)} disabled={wiping}>
                Cancel
              </Button>
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
