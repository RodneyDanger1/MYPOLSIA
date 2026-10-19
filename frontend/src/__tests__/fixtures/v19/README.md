# `fixtures/v19` — CAPTURED LIVE API PAYLOADS (verbatim, never hand-written)

These files are **not mocks**. Each one is the unmodified body of a read-only `GET` against the
running stack at `http://127.0.0.1/api/v1/...`, captured 2026-10-18 (stack up, `X-API-Key` read
from the repo `.env`, never printed). Captured by wave V19 / L4 (`l4-fe-contract`); the capture
script and the raw status log live in the lane dir:

    polsia-work/build/ui-reverse/wave-v19/l4-fe-contract/raw/fetch.sh
    polsia-work/build/ui-reverse/wave-v19/l4-fe-contract/raw/fetch-log.txt

Only `GET` was ever issued (no POST/PUT/PATCH/DELETE), and no fixture was edited afterwards — a
parser that cannot read one moves, the fixture does not. They are consumed by
`src/__tests__/v19-fe-contract.test.tsx` as the contract's red/green baseline.

| route | fixture | HTTP | bytes |
|---|---|---|---|
| `/dashboard/summary` | `dashboard-summary.json` | 200 | 298 |
| `/dashboard/activity?limit=20` | `dashboard-activity.json` | 200 | 11727 |
| `/dashboard/reports?limit=20` | `dashboard-reports.json` | 200 | 12241 |
| `/dashboard/reports/daily` | `dashboard-reports-daily.json` | 200 | 4 (`null`) |
| `/finance/summary` | `finance-summary.json` | 200 | 947 |
| `/finance/planning` | `finance-planning.json` | 200 | 1996 |
| `/channels/caps` | `channels-caps.json` | 200 | 807 |
| `/tasks?limit=50` | `tasks.json` | 200 | 65443 |
| `/tasks/1` | `task-1.json` | 200 | 377 |
| `/agents/status` | `agents-status.json` | 200 | 1247 |
| `/invoices` | `invoices.json` | 200 | 2568 |
| `/invoices?limit=100&status=draft` | (status-only probe; not stored) | 200 | — |
| `/schedules` | `schedules.json` | 200 | 282 |
| `/ads/campaigns?limit=20` | `ads-campaigns.json` | 200 | 2 (`[]`) |
| `/social/posts?limit=20` | `social-posts.json` | 200 | 695 |
| `/outreach/prospects?limit=20` | `outreach-prospects.json` | 200 | 2 (`[]`) |
| `/support/inbox?limit=20` | `support-inbox.json` | 200 | 2 (`[]`) |
| `/notifications` | `notifications.json` | 200 | 3466 |
| `/config` | `config.json` | 200 | 2489 |
| `/config/deepseek-key` | `config-deepseek-key.json` | 200 | 59 |
| `/ops/capabilities` | `ops-capabilities.json` | 200 | 4868 |
| `/ops/sandbox` | `ops-sandbox.json` | 200 | 21 |
| `/ops/agent-quality` | `ops-agent-quality.json` | 200 | 1259 |
| `/ops/events/stats` | `ops-events-stats.json` | 200 | 100 |
| `/planner/status` | `planner-status.json` | 200 | 1105 |
| `/auto-mode` | `auto-mode.json` | 200 | 31 |
| `/pricing?limit=50` | `pricing.json` | 200 | 17778 |
| `/runs?limit=20&offset=0` | `runs.json` | 200 | 8569 |
| `/chat?limit=50` | `chat.json` | 200 | 3927 |
| `/memory?q=invoice&limit=10` | `memory.json` | 200 | 15692 |

Notes that matter to the assertions:

* `/dashboard/reports/daily` answers a literal `null` (no report written yet) — the panel's honest
  "No report for today yet" state, not a broken read.
* `/finance/summary` carries measured `0`s (`mrr_cents: 0`) beside real `null`s
  (`total_ad_spend_usd: null`) — the null≠0 law has both sides in one body.
* `/tasks` and `/memory` are bare arrays; `/runs`, `/schedules`, `/notifications`,
  `/dashboard/reports` and `/chat` are `{items,total|count}` wrappers; `/ops/capabilities` is a bare
  map. All three shapes are exercised.
* `/memory` rows carry `created_at: null`, `tags: null`, `fallback: null`; `/chat` rows carry
  `task_id: null|number`; `/invoices` rows carry `tax/total/paid_at/bill_to: null`. The nullables
  the types declare are present as nulls on the wire.
* `/tasks?limit=50` legitimately repeats a title across scheduler rows — the board pin uses
  `findAllByText`, not a unique-text lookup.
* `/usage/summary` does **not** exist (404) and no FE code calls it; it was probed once to confirm
  the route is not part of this contract.
