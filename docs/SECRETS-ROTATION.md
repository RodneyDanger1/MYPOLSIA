# SECRETS — what holds a credential, and how to rotate it

Written by the SENTINEL lane (wave 5, 2026-10-28) after a live re-scan of the rebuilt stack. Every
statement here is measured against the running deployment; the commands are the ones used.

Two facts frame everything below:

1. **`API_KEY` is a build-time credential.** The dashboard sends `NEXT_PUBLIC_API_KEY` on every
   request and Next.js **inlines** `NEXT_PUBLIC_*` into the client bundle at build time. The served
   chunk `/_next/static/chunks/919-*.js` contains the exact owner key (verified: HTTP 200, 8,907
   bytes, the key as a literal). Any browser that can load the dashboard holds the whole owner API
   surface — mint approvals, read artifacts, confirm paid runs. **Rotating the backend key alone
   breaks every page** (each fetch 401s while `curl` still looks healthy, because curl sends no
   header); the frontend MUST be rebuilt with the same value.
2. **`POSTGRES_PASSWORD` has a committed default** (`polsia_password`, in `docker-compose.yml`,
   `.env.example` and `app/config.py`). Postgres publishes no host port, so it is not reachable
   from outside the compose network — but anything that gets a shell in the network has it.

Nothing in this file prints a secret value. Rotation steps use placeholders.

## Inventory (live, 2026-10-28)

| Secret | Where it lives | What it opens | Blast radius if leaked |
|---|---|---|---|
| `API_KEY` | `.env` (git-ignored) + **inlined in the frontend bundle** | the entire owner API: config PUT, approvals mint, artifacts read, chat confirm, ws ticket | full owner authority from any browser that loads the app |
| `APPROVAL_SECRET` | `.env` | mints/verifies approval tokens for gated (irreversible) actions | can mint an approval for any fingerprint it can compute → authorise one irreversible action per token |
| `POSTGRES_PASSWORD` | `.env` + committed default in compose/config/.env.example | the database over the compose network | read/write every table, incl. the append-only audit ledger |
| `DEEPSEEK_API_KEY` | `backend/data/deepseek_api_key.json` inside the `backend_data` volume (entered in Settings) + optional `.env` | paid model calls | spend on the owner's account |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | `.env` | payment reads, webhook signature | read financial data / forge webhooks |
| `RENDER_API_KEY`, `RENDER_SERVICE_ID` | `.env` (unset today) | live deploys of generated sites | deploy/replace the owner's sites |
| `SENDGRID_API_KEY` | `.env` (unset today) | outbound email | send as the owner's domain |
| `POLSIA_INBOUND_SECRET` | `.env` (blank = not configured) | the inbound-mail webhook | inject fake inbound email into the support inbox |
| `WS` tickets | derived (HMAC of a domain-separated `API_KEY`), 30 s TTL, never stored | one activity socket | nothing after 30 s; cannot be turned back into the key |

Containment status measured in wave 5: the DeepSeek key, `APPROVAL_SECRET`, both Stripe secrets and
the API key appear **nowhere outside their own store** — not in logs, not in the export archive, not
in any lane folder; `.env` is git-ignored (`.gitignore:36`). The two exposures that DO exist are the
inlined key (design, above) and the committed Postgres default (above).

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
docker compose up -d frontend backend worker
#    (backend/worker also read API_KEY at boot; both restart to pick up .env)

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
#    * nothing else stores the API key.
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
# 1. Change it IN the running server first (the role is `polsia` unless POSTGRES_USER overrides):
docker compose exec postgres psql -U polsia -d polsia -c \
  "ALTER USER polsia WITH PASSWORD '<new>';"

# 2. .env: POSTGRES_PASSWORD=<new>   (DATABASE_URL is composed from it in docker-compose.yml)
# 3. Recreate the services that read it:
docker compose up -d --force-recreate backend worker
# 4. Verify: /api/v1/ready -> 200 with checks.database = "ok"; alembic current shows 0018.
# 5. Drop the committed default from docker-compose.yml / .env.example / app/config.py so a fresh
#    clone cannot start with a known password (owner action: it also affects the backup sidecar,
#    which gets PGPASSWORD from the same variable).
```

A fresh volume (`docker compose down -v`) would re-init with the new value, but it destroys the
database — do not do that to rotate.

## Rotate `APPROVAL_SECRET`

```bash
# generate a new 64-hex value, put it in .env, `docker compose up -d --force-recreate backend worker`.
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
* Refusals and errors carry the `E_*` code and the fix hint, never the credential; `_redact_query_string`
  scrubs credential-bearing query pairs out of the ASGI scope before uvicorn can log them.
* The data export excludes the key store; the activity ledger records approvals by fingerprint and
  jti, never by token.

## Known residuals (owner decisions)

| # | Residual | Severity | Owner |
|---|---|---|---|
| R-W4-S2 / W5 | `API_KEY` is the guessable default `dev-key` **and** inlined in the served bundle | P2 (design) | owner: rotate, then plan the BFF (wave 7) |
| R-W4-S3 | committed `POSTGRES_PASSWORD` default in compose/config/.env.example | P3 | owner |
| W5 | `POSTGRES_PASSWORD` is also quoted (name + value) in three lane folders' evidence under `polsia-work/`; not usable without network access | P4 (info) | owner/integrator: acceptance, not a runtime leak |
