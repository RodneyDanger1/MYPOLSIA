import type { FinanceSummary } from "@/lib/api";

import { summaryCards } from "./summary";

/**
 * The seven KPI tiles: live label typography (mono-caps micro-label), large serif-ink value,
 * muted footnote — the widget language of the captured Business panel, no trend arrows (Q-23:
 * the old page passed inert `trend=\"up\"` props; a decoration that cannot be true is worse than
 * none). Values arrive pre-formatted from the pure `summaryCards` law; this component only lays
 * them out.
 */
export function FinanceSummaryGrid({ summary }: { summary: FinanceSummary }) {
  return (
    <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-4" data-testid="finance-kpis">
      {summaryCards(summary).map((cardSpec) => (
        <div
          key={cardSpec.key}
          className="card-surface px-3 py-3"
          data-testid="kpi-card"
          data-kpi-key={cardSpec.key}
        >
          <p className="ui-label text-ink-muted">{cardSpec.label}</p>
          <p className="mt-1 text-2xl font-bold text-ink">{cardSpec.value}</p>
          {cardSpec.note ? <p className="mt-1 text-xs text-ink-muted">{cardSpec.note}</p> : null}
        </div>
      ))}
    </div>
  );
}
