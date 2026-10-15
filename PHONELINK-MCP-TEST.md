# PHONELINK MCP TEST — standalone status + test sheet
**Label:** PHONELINK MCP TEST · **Status:** WORKS (Hermes-side) · **In the Polsia app:** **shipping in
wave V16** — the read-only Polsia integration (items P-01…P-06 of `PHONE-LINK-AMENDMENT.md`) lands this
wave: backend mount + `phone_link_service` + `GET /api/v1/phone/*` routes + orchestrator activity feed +
dashboard Phone card + `/phone` page + streaming export. In flight at the time of writing; this line flips
to "in the product" with the wave's live-drill evidence at settle.

## What this is
A read-only bridge from the assistant stack to Riley's phone, via the **Windows Phone Link** app's local
stores (no cloud, no third-party service, no phone-app installation):

- Skill + scripts: `%LOCALAPPDATA%\hermes\skills\phone-link-bridge\scripts\phone_link.py` (CLI, 13
  subcommands) and `phone_link_mcp.py` (MCP stdio server).
- Registered MCP server name: **`phone-link`** (10 tools): `sms_recent`, `sms_search`, `sms_codes`,
  `sms_unread`, `phone_thread`, `calls_list`, `phone_notifications`, `phone_toasts`, `phone_export`,
  `phone_media_export`.
- Data sources (read-only SQLite): Phone Link `phone.db` (SMS/MMS/threads/contacts), `calling.db`
  (call history), `notifications.db`, and the Windows toast DB (`wpndatabase.db`) for Phone-Link toasts.
  Store root: `%LOCALAPPDATA%\Packages\Microsoft.YourPhone_8wekyb3d8bbwe\LocalCache\Indexed\<GUID>\System\Database\`
  (the `<GUID>` is per-machine; Polsia's backend resolves it from the `PHONE_LINK_DB_DIR` env var — see below).
- Exports write ONLY to `Desktop\PhoneLink-Archive\` (full archive: messages.json/html, calls.csv,
  contacts.json, summary.md; media/ = MMS attachments).

## How to test (takes ~1 minute)
1. `hermes mcp test phone-link` → expect: connected, 10 tools discovered.
2. `python <skill>\scripts\phone_link.py stats` → store census (SMS/MMS/threads/calls/contacts counts).
3. `python <skill>\scripts\phone_link.py codes 3 --within 48` → recent 2FA codes (SMS+MMS).
4. `python <skill>\scripts\phone_link.py export` → archive lands under `Desktop\PhoneLink-Archive\<stamp>\`.
5. In a Hermes session after `/reload-mcp` (or any new session): ask "check my unread texts" — the
   `sms_unread` tool answers with real data.

## Honest limits (why, not hand-waving)
- **READ-ONLY**: every open is `mode=ro`; the store belongs to the Phone Link app. Nothing here (and
  nothing in the Polsia integration) can modify, delete, or fake a message.
- **SENDING IS NOT SUPPORTED** (no local send API exists). The only real send path is pairing Google
  Messages web to this PC and driving the visible browser (`messages.google.com/web`) — offered, not built.
- Windows-only; the Phone Link app must be running for fresh sync.
- 2FA codes: fetched only when a flow needs them; never printed into public logs/documents. In Polsia,
  `GET /api/v1/phone/codes` is the one route that is **key-only and audit-logged** (an `activity_log` row
  per call) — it exists for auth flows, not dashboards.
- Voicemail: missed/unanswered calls are visible; voicemail audio/transcripts are not in the local store
  (carrier-dependent — voicemail-to-text messages would arrive as normal SMS).

## How it enters Polsia (shipping in wave V16)
- **P-01** `docker-compose.yml` bind-mounts the store **read-only** into `backend` and `celery_worker`
  at `/phone-link:ro`, host path from env **`PHONE_LINK_DB_DIR`** (blank = the measured Windows default,
  the `YourPhone_…` path above); new `app/services/phone_link_service.py` ports the read logic
  (SMS/MMS union, FILETIME→local conversion, contact-name resolution, call-type legend) and **refuses by
  name (`E_PHONE_UNAVAILABLE`, 503)** when the mount/store is absent — never fabricates.
- **P-02** routes (key-auth, `{ok, data}` envelope, coded refusals):
  `GET /api/v1/phone/summary` · `GET /api/v1/phone/messages?limit&thread_id` ·
  `GET /api/v1/phone/calls?limit&missed_only` · `GET /api/v1/phone/codes?within_hours` (key-only +
  audit-logged) · `GET /api/v1/phone/export` (streaming zip: messages.json, calls.csv, contacts.json +
  `Content-Disposition` filename).
- **P-03** a ≤5-minute maintenance-style tick publishes "SMS received: <who>" / "Missed call: <who>"
  into the existing activity pub/sub, **deduped by (kind, id)** — the orchestrator and the activity feed
  see phone events; the watermark is persisted honestly.
- **P-04/P-05** dashboard **Phone** card (unread, last messages, missed calls today; honest
  `E_PHONE_UNAVAILABLE` state) + the **`/phone`** page (recent/thread view/search/calls + archive
  download).
- **P-06** doctrine + docs (this file, USAGE.md §5, README).
- No send path — unchanged by this wave (see limits above).
Full spec: `PHONE-LINK-AMENDMENT.md` in the campaign records (`polsia-work/build/ui-reverse/wave-v15/PLAN/`).
The Hermes-side MCP remains the way HERMES sessions read the phone; the Polsia side is the app's own
native read path because backend containers cannot call a Hermes MCP.
