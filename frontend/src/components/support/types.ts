/**
 * The wire rows of `/api/v1/support/inbox` — literally `SupportInboxItemResponse`
 * (`backend/app/schemas/support.py:27-36`). The frontend has no generated client, so this is a
 * hand copy of the schema; the u11-support suite pins the fields the UI renders.
 *
 * Nullability is the schema's own: `received_at` / `task_id` / `created_at` are `| None`
 * server-side and are rendered as their honest absences, never defaulted.
 */
export type SupportInboxItem = {
  id: number;
  provider_message_id: string;
  from_address: string;
  subject: string;
  body: string;
  status: string;
  received_at: string | null;
  task_id: number | null;
  created_at: string | null;
};

/** The `?status=` filter values — `support_inbox_service.VALID_STATUSES` ("pending",
 *  "dispatched"). An unknown value is refused by the service by name (422), so the filter
 *  offers exactly these. */
export type SupportInboxStatus = "pending" | "dispatched";
