"""The Polsia agent layer — runtime, registry and the nine agents.

Package map (all lifted from lane 05's proven runtime, wired to `app.config.settings` and
`app.core.errors`):

| Module | Responsibility |
|---|---|
| `claude_cli` | the ONLY place that builds a CLI argv: transport guards (argv budget, stdin), retry ladder, concurrency bound, named `E_CLI_*` failures |
| `structured_output` | envelope → validated payload: the POLSIA-Exxx taxonomy, deterministic repairs, the bounded repair ladder, and the POLSIA→`E_*` mapping |
| `context_assembler` | the eight named prompt layers, per-layer token budgets from `planner_context_token_budget`, cache-friendly stable prefix, secret screen |
| `accounting` | envelope → `RunUsage`; resume-safe cost deltas; the pre-dispatch spend gate (`authorize`/`BudgetStop`) |
| `policy_firewall` | per-agent allowlists, untrusted-content quarantine, rate caps, approval tokens, kill switch |
| `side_effects` | the sandbox contract: a refused side effect becomes a labelled DRAFT, never a fake success |
| `integrations` | the real provider call sites (lazy imports, named `E_INTEGRATION` when unconfigured) |
| `base_agent` | `BasePolsiaAgent` — the test-pinned `call_claude`/`call_claude_json`/`timed_run` contract, wired to the runtime above, attaching `RUN_META_KEYS` for `agent_runs` |
| `crew_factory` | `AGENT_MAP` (module, class) registry with lazy import, kill-switch preflight and `timed_run` dispatch |

Every agent's returned dict carries the accounting keys in `base_agent.RUN_META_KEYS` so the queue
can persist tokens/cost/session into `agent_runs`; on failure the agent raises a `PolsiaError`
whose `code` is the `E_*` name and whose `context["failure_code"]` is the stable POLSIA-Exxx.

Import-free by design: nothing here imports tweepy/stripe/sendgrid/PyGithub/chromadb/asyncpg at
module level (CLAUDE.md:116).
"""
