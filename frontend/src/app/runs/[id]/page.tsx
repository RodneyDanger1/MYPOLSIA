"use client";

import { useState } from "react";
import Link from "next/link";

import { ListSkeleton, ReadError } from "@/components/finance/states";
import { RunDetail } from "@/components/runs/RunDetail";
import { parseRunId } from "@/components/runs/parseRunId";
import { useRunDetail } from "@/components/runs/useRunDetail";

/**
 * `/runs/[id]` — the addressable run detail (V21/L2).
 *
 * The read is the real `GET /api/v1/runs/{id}` (backend `app/api/v1/runs.py`): the frozen ledger
 * row + `num_turns` / `is_error`, the only envelope facts the record stores.  `tools_used` is
 * deliberately NOT part of that contract — the record does not store it — so this page renders
 * the honest "not recorded" panel instead of fabricated chips (see `RunDetail.tsx`).
 *
 * States follow the house idiom (`/tasks/[id]`, `/usage`): loading is a busy skeleton, the
 * failure is NAMED through the shared `ApiError` display via `ReadError`, and a `[id]` that is
 * not a positive integer is never requested — the page says what the route expects.
 *
 * The ledger link is a plain `<a>` (a full page load), the same choice `/tasks/[id]` makes: this
 * route works from any render context.
 */

export default function RunDetailPage({ params }: { params: { id: string } }) {
  const runId = parseRunId(params?.id);
  const [reloadSeq, setReloadSeq] = useState(0);
  const { data, error, loading } = useRunDetail(runId, reloadSeq);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 border-b border-line pb-2">
        <Link href="/usage" className="ui-label text-link hover:underline">
          <span aria-hidden>‹</span> Usage ledger
        </Link>
        {runId !== null ? (
          <span className="ui-label text-ink-muted">Run #{runId}</span>
        ) : null}
        <button
          type="button"
          className="link ui-label ml-auto"
          onClick={() => setReloadSeq((n) => n + 1)}
          disabled={runId === null}
        >
          Refresh
        </button>
      </div>

      {runId === null ? (
        <div role="alert" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Not a run id</p>
          <p className="mt-1 text-sm text-ink">
            This route takes one positive numeric run id, and the address bar carries &ldquo;
            {params?.id ?? ""}&rdquo;. Nothing was requested; open a run from{" "}
            <a href="/usage" className="link">
              the usage ledger
            </a>{" "}
            instead.
          </p>
        </div>
      ) : null}

      {runId !== null && error !== null ? (
        <ReadError label="this run" message={error} />
      ) : null}

      {runId !== null && loading ? <ListSkeleton testId="run-detail-loading" /> : null}

      {runId !== null && data !== null ? <RunDetail run={data} /> : null}
    </div>
  );
}
