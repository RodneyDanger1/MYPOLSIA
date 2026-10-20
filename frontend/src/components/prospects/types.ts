/**
 * The wire types of `/api/v1/prospects` (V27/L5), declared HERE rather than in `src/lib/api.ts`
 * because this lane owns `components/prospects/**` and must not edit the shared client module.
 * Field-for-field with `app/schemas/social.ProspectResponse` + `app/schemas/prospect.py`.
 */

/** One stored prospect. Every optional field is nullable on the wire; null means "not recorded"
 *  and renders as the em dash — never an empty cell and never an invented value. */
export type Prospect = {
  id: number;
  email: string;
  first_name: string | null;
  last_name: string | null;
  company: string | null;
  title: string | null;
  source: string | null;
  /** Open string on the wire (`str | None`): the words in `PROSPECT_STATUSES` are the values the
   *  existing rows use, NOT a backend enum — an unknown word renders as itself, null as "—". */
  status: string | null;
  created_at: string | null;
};

/** `GET /prospects` — the page plus the server's FULL match count (never the page length). */
export type ProspectList = { items: Prospect[]; total: number };

/** `POST /prospects` body. An absent optional field is omitted; a blank one is not sent either
 *  (the service stores NULL, and the page must not claim a name the operator did not type). */
export type ProspectCreate = {
  email: string;
  first_name?: string;
  last_name?: string;
  company?: string;
  title?: string;
  source?: string;
  status?: string;
};

/** `PATCH /prospects/{id}` body — only the keys the operator changed. An explicit `null` CLEARS
 *  a nullable column (the backend's `exclude_unset` + `null` semantics), which is how the edit
 *  form erases a value the operator emptied. */
export type ProspectUpdate = {
  email?: string;
  first_name?: string | null;
  last_name?: string | null;
  company?: string | null;
  title?: string | null;
  source?: string | null;
  status?: string | null;
};

/**
 * The status words the EXISTING rows carry (`ck_prospects_status` in migration 0002; the same
 * tuple `app/api/v1/status_filters.py` serves the outreach read). This is UI vocabulary for the
 * filter and the status select — the row renderer keeps an unknown stored word as itself, and the
 * select adds a stored-but-unknown value as an option instead of dropping it.
 */
export const PROSPECT_STATUSES: readonly string[] = [
  "new",
  "contacted",
  "replied",
  "converted",
  "unsubscribed",
  "bounced",
];
