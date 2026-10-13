# AGENTS.md — operating rules for agents working in this repository

Polsia: a FastAPI + Celery backend with a Next.js dashboard; the agent runtime is DeepSeek-first
(V4.1 Flash via the OpenAI-compatible API; the key is entered in the app and stored server-side)
with the Claude Code CLI as a fallback (`README.md` has the architecture). This file is the repository's own instruction set
for AI agents — the rules, the gates, and where the evidence goes. Read it before your first
command. `README.md` describes the product and the verified no-Docker run; `CLAUDE.md` holds the
code conventions (fixtures, SQLite-compat rules, model imports, how to add an agent/endpoint).

## Ground rules (the ones the code already enforces)

- **No fake success, ever.** A capability that does not exist returns a NAMED `E_*` refusal with a
  `howToFix` (`app/core/errors.py`, `PolsiaError`) — never an empty list for "could not read", never
  a `0` for "unknown", never a success reported before the work that would prove it. Why: this
  repository's defect history is almost entirely work that looked correct and was not.
- **Numbers are measured or they say unknown.** Gate counts come from the JUnit XML (below) — never
  from the `-q` output, never from prose. Why: `-q` is already in pytest's `addopts`; a second `-q`
  paints dots and exits 0 with no counts at all.
- **A skip is never green.** A skipped check is reported as skipped, by name and reason — not a pass
  and not a failure quietly absorbed. Why: a check that cannot fail, or was never run, proves nothing.
- **New behaviour ships with a RED leg.** Plant the defect, show the named check fail, restore,
  re-run green, and paste the raw output. Why: a test you have never seen fail may not test anything.
- **Never weaken, delete or skip a test to land a change.** Repairing a check that pins wrong
  behaviour is allowed only when the diff comment cites the old and the new arithmetic. Why: silence
  is how a finding dies.
- **No mock capability in the product.** `CLAUDE_CLI_MOCK=true` is for tests and CI only; product
  paths call the real runtime (DeepSeek-first, Claude CLI fallback) or refuse by name. Why: a mock that ships is a gate that cannot fire.

## The gates

The repo venv is `.venv/` at the root; `python` and `pytest` are NOT on PATH in git-bash, so call
the interpreter explicitly. Backend gates run from `backend/`:

```bash
# THE gate: unit + parity + red-team. Write the XML where your receipts go; read the count from it.
CLAUDE_CLI_MOCK=true ../.venv/Scripts/python.exe -m pytest tests/unit tests/parity tests/redteam -q -p no:cacheprovider --junitxml=junit.xml

# counts, from the XML only (junit.xml is name-ignored by .gitignore)
../.venv/Scripts/python.exe -c "import sys, xml.etree.ElementTree as ET; r = ET.parse(sys.argv[1]).getroot(); print({k: sum(int(t.get(k, 0)) for t in r.iter('testsuite')) for k in ('tests', 'failures', 'errors', 'skipped')})" junit.xml

# lint under BOTH pins — the CI pin and the venv's; both must be clean
../.venv/Scripts/python.exe -m ruff check app/ celery_app/ tests/
uvx --from ruff==0.8.4 ruff check app/ celery_app/ tests/

# types (advisory in CI; run before a hand-off)
../.venv/Scripts/python.exe -m mypy app/

# coverage — the suite must clear the ratchet in pyproject.toml (fail_under)
CLAUDE_CLI_MOCK=true ../.venv/Scripts/python.exe -m pytest tests/unit -q -p no:cacheprovider --cov=app --cov-report=term-missing
```

Frontend gates run from `frontend/`:

```bash
npx tsc --noEmit -p tsconfig.json      # type check
npx next lint --max-warnings=0         # eslint
npm test -- --watchAll=false           # jest
NEXT_PUBLIC_API_KEY=dev-key NEXT_PUBLIC_API_URL=http://localhost npm run build   # production build
```

Environment check, from the repo root: `.venv/Scripts/python.exe check_deps.py` — every module the
suite needs must be importable (`MISSING (0)`).

A red gate may not be yours. Other writers may be mid-flight; separate your changes before believing
the failure (`git status --short`, `git diff`) — another owner's half-written state is not yours to
"fix".

## Working in the tree

- **One writer per file.** If you need a file you do not own, write a `## REQUESTS` entry (the failing
  command + its raw output + the proposed change) and build everything you can behind the interface.
  Never edit it "just this once". Why: two writers on one file is how the cross-lane seams happened.
- **Git writes belong to the integrator.** `add`, `commit`, `checkout`, `stash`, `reset` are not
  yours; read-only git (`status`, `log`, `diff`, `show`) is encouraged. Your work ends with a clean
  `git status --short` for your own files and a receipt. Why: lanes leave evidence; the owner
  integrates.
- **Receipts or it did not happen.** Paste raw command output; name every `UNRUN` item with its
  reason; state every claim that changed. A receipt has seven sections — goal · files with shas ·
  commands with output · RED leg · acceptance · not-done · requests — and §6 (not done) is never
  empty. Why: an empty §6 means the author did not look.
- **Receipts live OUTSIDE the repository**, in the lane directory the delegator gives you as an
  absolute path (this workspace: `C:\Users\Riley\Desktop\polsia-work\build\<lane>\` — `PLAN.md`,
  `raw/`, `RECEIPT.md`). Never create a campaign directory inside the repo. Why: scratch inside the
  deliverable is how a stray `polsia-work/` once appeared in a tree.
- **Flush to disk as you go.** Write each finding, signature and command output into the receipt as
  you produce it. Why: an interrupted lane keeps only what it already wrote.
- **Back up exact bytes** of every file you edit into your own lane dir before the edit, with sha256
  before and after. Why: a restore must be byte-exact, not "close enough".
- **Do not touch the venv** (`.venv/`). For a one-off tool run use `uvx tool==version`. Why: the venv
  is the measured environment; a "repaired" venv invalidates every measurement taken on this host.

## Host rules (Windows git-bash — each one measured)

- **Native tools need `C:/...` paths.** An MSYS `/c/...` path arrives as a literal `C:\c\Users\...`
  and fails with "can't open file" / "cannot change to". Pass `C:/...`.
- **Kill with PowerShell** `Stop-Process -Id <pid> -Force`; `taskkill //F //PID` is refused here.
- **Build/serve order: stop → build → serve.** Never run `npm run build` while a standalone server
  is up — Windows keeps `.next/standalone` as that process's cwd and the build stalls with no output
  past the banner. Serve the prebuilt tree with `npm start` (it runs `scripts/start-standalone.mjs`;
  `next start` refuses `output: standalone`). The full no-Docker sequence is README.md, "Running the
  stack without Docker".
- **Subprocesses get a CLEAN environment.** If a script spawns `../.venv/Scripts/python.exe`, pass
  only `PATH`, `SYSTEMROOT`, `TEMP`, `TMP`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA`, `PATHEXT`,
  `COMSPEC`. Why: the harness's `PYTHONPATH` otherwise wins and you get
  `ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'` for a file that is present
  and hash-correct — an environment bug, not a broken install. Do not "repair" the venv.
- **When Docker is absent (as on this host)**, the compose-based `make` targets (`up`, `down`,
  `test*`, `lint`, ...) cannot run; use the no-Docker path in README.md.
- **PostgreSQL-only semantics are `environment-only`.** `FOR UPDATE SKIP LOCKED`, CHECK constraints,
  the append-only trigger and `Numeric` rendering are not exercised on SQLite — `alembic upgrade
  head` on SQLite skips each with a printed reason. Name them; do not simulate them.

## Where the truth lives

`README.md` (the product + the verified local run) · `CLAUDE.md` (conventions) ·
`docs/ACCEPTANCE_TESTING.md` (the acceptance plan) · `backend/tests/` (unit, parity, red-team,
integration). When a document and the code disagree, the code wins — and the disagreement is
recorded as a finding, not silently resolved.
