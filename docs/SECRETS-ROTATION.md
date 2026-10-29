# SECRETS — what holds a credential, and how to rotate it

Written by the SENTINEL lane (wave 5, 2026-10-28); containment re-measured in wave 6 (2026-10-29)
against the rebuilt wave-6 image. Every statement here is measured against the running deployment;
the commands are the ones used.

Two facts frame everything below:

1. **`API_KEY` is a build-time credential.** The dashboard sends `NEXT_PUBLIC_API_KEY` on every
   request and Next.js **inlines** `NEXT_PUBLIC_*` into the client bundle at build time. The served
   chunk `/_next/static/chunks/919-*.js` contains the exact owner key (verified: HTTP 200, 8,907
   bytes, the key as a literal). Any browser that can load the dashboard holds the whole owner API
   surface — mint approvals, read artifacts, confirm paid runs. **Rotating the backend key alone
   breaks every page** (each fetch 401s while `curl` still looks healthy, because curl sends no
   header); the frontend MUST be rebuilt with the same value.
2. **`POSTGRES_PASSWORD` has a committed default** (`meridian_password`, in `docker-compose.yml`,
   `.env.example` and `app/config.py`). Postgres publishes no host port, so it is not reachable
   from outside the compose network — but anything that gets a shell in the network has it.

Nothing in this file prints a secret value. Rotation steps use placeholders.

## Inventory (live, re-measured 2026-10-29)

| Secret | Where it lives | What it opens | Blast radius if leaked |
|---|---|---|---|
| `API_KEY` | `.env` (git-ignored) + **inlined in the frontend bundle** | the entire owner API: config PUT, approvals mint, artifacts read, chat confirm, ws ticket | full owner authority from any browser that loads the app |
| `APPROVAL_SECRET` | `.env` | mints/verifies approval tokens for gated (irreversible) actions | can mint an approval for any fingerprint it can compute → authorise one irreversible action per token |
| `POSTGRES_PASSWORD` | `.env` + committed default in compose/config/.env.example | the database over the compose network | read/write every table, incl. the append-only audit ledger |
| `DEEPSEEK_API_KEY` | `backend/data/deepseek_api_key.json` inside the `backend_data` volume (entered in Settings) + optional `.env` | paid model calls | spend on the owner's account |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | `.env`; the webhook secret ALSO has a leaked copy in `wave-v10/stripe-lab/listen.log` (W6-S1) | payment reads, webhook signature | read financial data / forge webhooks |
| `RENDER_API_KEY`, `RENDER_SERVICE_ID` | `.env` (unset today) | live deploys of generated sites | deploy/replace the owner's sites |
| `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL` | `.env`, the settings secret store, or Settings → Integrations (the send path resolves stored > env > Settings; wave 7 F-W7-01/F-47) | outbound email | send as the owner's domain |
| `SUPPORT_INBOUND_SECRET` | `.env` (blank = not configured) | the owner/JSON inbound-mail webhook | inject fake inbound email into the support inbox |
| `SENDGRID_INBOUND_PUBLIC_KEY`, `SENDGRID_INBOUND_OAUTH_TOKEN` | `.env` / secret store (blank = not configured) | SendGrid Inbound Parse webhook signature (ECDSA P-256, 300 s tolerance) or OAuth bearer | forge an inbound delivery as SendGrid (otherwise 401 `E_AUTH`) |
| `WS` tickets | derived (HMAC of a domain-separated `API_KEY`), 30 s TTL, never stored | one activity socket | nothing after 30 s; cannot be turned back into the key |

Containment status re-verified in waves 7–8 (2026-10-29, wave-7 build live for wave-8 probes):
tracked files + the lane's artefacts + backend/worker/nginx logs:

* **Clean outside their own store:** `APPROVAL_SECRET`, the **current** `DEEPSEEK_API_KEY`,
  `STRIPE_SECRET_KEY`, `SENDGRID_API_KEY`, `RENDER_API_KEY` — **0 hits** each.
* **`API_KEY`** is the documented default `dev-key` **and** inlined in the served bundle
  (`/_next/static/chunks/919-*.js`, HTTP 200, 8,907 B, verified again this wave). The chunk name is
  build-specific: re-scan `/_next/static/chunks/*.js` after any frontend build. The default value
  also appears in docs/tests/compose by design.
* **`POSTGRES_PASSWORD`** is still the committed default (`meridian_password`), in compose/config/
  `.env.example`/tests and copies — the known P3.
* **`STRIPE_WEBHOOK_SECRET` is LEAKED to disk (wave-6 finding):** the live value (70 chars,
  sha256_8 `ac7cab10`) appears verbatim in `wave-v10/stripe-lab/listen.log` — a git-ignored,
  repo-local lab log (plus helper scripts beside it). Treat as leaked: rotate the webhook secret in
  the Stripe dashboard, update `.env`, recreate `backend`/`celery_worker`, then delete the lab log.
* **`backups/` (git-ignored, repo-local) is a secret+PII store (wave-6 finding):**
  `backend_data.tgz` holds the **previous DeepSeek key** (35 chars, different from the live key) and
  the ten **un-redacted legacy `contact.html` pages**; the three `meridian-*.sql` dumps hold the
  owner's street/locality/legal name from before the profile correction. Nothing here is committed,
  but a backup copied off-host carries them: revoke the old DeepSeek key at the provider if it is
  still live, keep the archives encrypted, and never attach them to a receipt.
* **Query-string credentials: FIXED LOG-SIDE (waves 7–8; W6-S3 + F-45).** `app/core/query_scrub.py`
  is called by an ASGI middleware for HTTP and by the WS handler; uvicorn's access log is disabled
  (`--no-access-log`; nginx owns access logging) and nginx logs a path-only request line with the
  referer's query stripped by a `map`. Verified live: a fresh `?api_key=`/`?token=` marker appears in
  NEITHER backend NOR nginx logs, while the public unsubscribe route still reads its token from the
  URL (a token cannot be stripped pre-routing). Residual **F-45-R1**: nginx's *error_log* can still
  print a raw request line on error entries — all observed lines are pre-fix; the fix is
  `error_log crit` (loses diagnostics) or moving WS auth off the query string (owner decision).
  Rotate anyway before exposing the app: a log line outlives a rotation.
* `.env` is git-ignored (`.gitignore:36`); the lane's redaction backup (`raw/w6/redaction-backup`)
  is the byte-exact **owner-gated** restore source and must not be deleted with scratch.

## Rotate `API_KEY` (and keep the dashboard working)

The backend and the frontend bundle must change **together**. Order matters.

```bash
# 0. What you need: a generated value, 32+ chars.  Do NOT reuse a dictionary word ("dev-key" is
#    the documented default and is guessable — it is also what this install still runs today).
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 1. Put the SAME value in both places the build/run read:
#    .env:  API_KEY=<new>   and   NEXT_PUBLIC_API_KEY=<new>
#    (NEXT_PUBLIC_API_KEY is a compose BUILD arg — see docker-compose.yml, frontend.build.args)

# 2. Rebuild the frontend (the key is inlined; an env change alone does nothing):
docker compose build frontend
docker compose up -d frontend backend celery_worker
#    (backend and celery_worker also read API_KEY at boot; both restart to pick up .env)

# 3. Verify from the SERVER (no header):  GET /api/v1/health  -> 200  (unauthenticated by design)
#    Verify with the NEW key:             GET /api/v1/artifacts -H "X-API-Key: <new>"  -> 200
#    Verify the OLD key is dead:          same call with the old value -> 401 E_AUTH
#    Verify the browser: load /dashboard; every panel must render DATA (not its empty state).
#      A blank dashboard + a healthy curl is the classic inlined-key mismatch (CLAUDE.md).
#    Verify the bundle no longer carries the old value:
#      fetch /_next/static/chunks/*.js and grep for the OLD key -> 0 hits.

# 4. Effects of the rotation (know before you do it):
#    * every outstanding ws ticket dies instantly (they are HMAC-derived from API_KEY) — harmless,
#      they live 30 s;
#    * the browser's open sockets reconnect and mint fresh tickets;
#    * nothing else stores the API key (the new frontend build inlines the new value; only old
#      query-string log lines could still hold the OLD value — see W6-S3);
```

**Do not** "fix" a mismatch by setting the frontend key back to the old value; that re-publishes the
leaked credential. Rotate both sides.

### The durable fix (not built; wave 7 territory)

A build-time credential in a public bundle cannot be rotated cheaply. The replacement is a
same-origin BFF/proxy that holds the key server-side (or a per-browser session cookie) so the
browser never receives it. Until then, treat the served bundle as public: this stack binds
`127.0.0.1:80` only and runs `SANDBOX_MODE=true`, which is what keeps the exposure bounded.

## Rotate `POSTGRES_PASSWORD`

The password is baked into the Postgres **data volume** at first init and into `DATABASE_URL` for
backend/worker. Changing `.env` alone locks the app out; you must change the role's password too.

```bash
# 1. Change it IN the running server first (the role is `meridian` unless POSTGRES_USER overrides):
docker compose exec postgres psql -U meridian -d meridian -c \
  "ALTER USER meridian WITH PASSWORD '<new>';"

# 2. .env: POSTGRES_PASSWORD=<new>   (DATABASE_URL is composed from it in docker-compose.yml)
# 3. Recreate the services that read it:
docker compose up -d --force-recreate backend celery_worker
# 4. Verify: /api/v1/ready -> 200 with checks.database = "ok"; alembic current shows **0020** (head).
# 5. Drop the committed default from docker-compose.yml / .env.example / app/config.py so a fresh
#    clone cannot start with a known password (owner action: it also affects the backup sidecar,
#    which gets PGPASSWORD from the same variable).
```

A fresh volume (`docker compose down -v`) would re-init with the new value, but it destroys the
database — do not do that to rotate.

## Rotate `APPROVAL_SECRET`

```bash
# generate a new 64-hex value, put it in .env, `docker compose up -d --force-recreate backend celery_worker`.
# Effect: every unspent approval token stops verifying (fail closed).  Parked tasks keep their
# fingerprint and need a FRESH mint — that is the documented "one approval, one attempt" direction,
# not a regression.  In-flight approvals are expected to be re-minted; there is no way to migrate
# them (the secret is the only key).
```

## Rotate the DeepSeek key

It is entered in Settings and stored server-side (`backend/data/deepseek_api_key.json`, inside the
`backend_data` volume). Rotation = re-enter it in Settings (which rewrites the file); the old value
is not retained anywhere else. It never appears in logs or the export (`secret_store` serialises a
masked form). If the key file is ever copied out of the volume, treat it as leaked: revoke in the
DeepSeek console first, then re-enter.

## What this repository already does for you

* `.env` is git-ignored; `.env.example` carries placeholders only.
* Secrets are read at runtime from env / the secret store; none is hard-coded in a route or an agent.
* Refusals and errors carry the `E_*` code and the fix hint, never the credential; the shared query scrub
  (`app/core/query_scrub.py`) covers HTTP and WS, uvicorn's access log is off, and nginx logs a path-only
  request line with a query-stripped referer (wave 7; see W6-S3 above). The key still belongs in
  `X-API-Key`, never a URL.
* The data export excludes the key store; the activity ledger records approvals by fingerprint and
  jti, never by token.

## Known residuals (owner decisions)

| # | Residual | Severity | Owner |
|---|---|---|---|
| R-W4-S2 / W5 | `API_KEY` is the guessable default `dev-key` **and** inlined in the served bundle | P2 (design) | owner: rotate, then plan the BFF (wave 7) |
| R-W4-S3 | committed `POSTGRES_PASSWORD` default in compose/config/.env.example | P3 | owner |
| W5 | `POSTGRES_PASSWORD` is also quoted (name + value) in three lane folders' evidence under `meridian-work/`; not usable without network access | P4 (info) | owner/integrator: acceptance, not a runtime leak |
| **W6-S1** | **live `STRIPE_WEBHOOK_SECRET` in `wave-v10/stripe-lab/listen.log`** (git-ignored, repo-local); helper scripts beside it | **P2 (leak)** | owner: rotate at Stripe first, update `.env`, recreate backend/celery_worker, delete the lab log |
| **W6-S2** | `backups/backend_data.tgz` holds the **previous DeepSeek key** and the ten un-redacted legacy `contact.html` pages; `backups/meridian-*.sql` hold the pre-correction owner address/legal name | P2 (leak, offline) | owner: revoke the old DeepSeek key if still live, encrypt/rotate the archives, never publish them |
| **W6-S3** | a credential in a **query string** was written verbatim to uvicorn/nginx access logs | **FIXED** (wave 7–8, log-side: uvicorn `--no-access-log`, nginx path-only + referer map, shared query scrub) | verified live (0 markers in both logs); rotate the default API_KEY before exposure |
| **F-45-R1** | nginx **error_log** may still print a raw request line (query included) on *error* entries | P3 (latent) | owner: `error_log crit` (loses diagnostics) or move WS auth off the query string |
| **W7-S1** | one lane's `docker compose config --format json` printed live `APPROVAL_SECRET`, the Stripe test key and the webhook secret into a tool transcript (no product source/log; scan 0 hits) | P3 (hygiene) | owner: rotate `APPROVAL_SECRET` alongside W6-S1; never dump compose config into evidence |
