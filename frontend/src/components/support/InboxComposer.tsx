"use client";

import { useId, useState } from "react";

import { Button } from "@/components/ui";
import { ApiError, api } from "@/lib/api";

import type { SupportInboxItem } from "./types";

/**
 * The inbox composer — the operator's half of `POST /api/v1/support/inbox`
 * (`backend/app/api/v1/support.py:31-45`), the ingest that closes the customer-support producer
 * chain (RD12's consumer had no producer; this is it, per the wiring audit's F4).
 *
 * Fields are the REAL wire shape (`SupportInboxItemCreate`):
 *   * `provider_message_id` — required; the provider's own message id, the idempotency key. A
 *     repeated id re-serves the STORED row (201 again, unchanged) — the copy says so, because
 *     "created" would be a claim the at-least-once contract cannot make;
 *   * `from_address`, `subject`, `body` — required (the service refuses blanks by name);
 *   * `received_at` — optional. Left blank it is NOT sent (`null` = "not stated" — never
 *     invented). When stated, the `datetime-local` value is converted to its instant with an
 *     offset before POSTing: the service REFUSES a timezone-less value by name (it would be
 *     read as UTC and silently reorder the attach), so shipping the raw wall-clock string would
 *     guarantee a refusal.
 *
 * Refusals render `ApiError.display` (code + message + how_to_fix) exactly as the server
 * spelled them — including the tz-less/length/blank refusals the service raises.
 */

type Notice = { tone: "status" | "alert"; text: string };

const EMPTY_FORM = {
  provider_message_id: "",
  from_address: "",
  subject: "",
  body: "",
  received_at: "",
};

const INPUT_CLASS = "w-full border border-line-strong bg-surface px-3 py-2 text-sm text-ink";

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

export function InboxComposer({ onFiled }: { onFiled?: () => void }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const idField = useId();
  const fromField = useId();
  const subjectField = useId();
  const bodyField = useId();
  const receivedField = useId();

  const complete =
    form.provider_message_id.trim() !== "" &&
    form.from_address.trim() !== "" &&
    form.subject.trim() !== "" &&
    form.body.trim() !== "";
  const canSubmit = !busy && complete;

  const set = (key: keyof typeof EMPTY_FORM) => (value: string) =>
    setForm((current) => ({ ...current, [key]: value }));

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setNotice(null);
    try {
      // The stated arrival becomes its instant BEFORE the wire (see the file header: the
      // service refuses a tz-less value by name). An unparsable entry is refused here, where
      // it can be explained, instead of being posted as a server refusal.
      let receivedAt: string | undefined;
      if (form.received_at.trim() !== "") {
        const when = new Date(form.received_at);
        if (Number.isNaN(when.getTime())) {
          setNotice({
            tone: "alert",
            text: "received_at could not be read as a datetime — fix it or clear the field (blank means “not stated”).",
          });
          setBusy(false);
          return;
        }
        receivedAt = when.toISOString();
      }

      const item = await api.post<SupportInboxItem>("/support/inbox", {
        provider_message_id: form.provider_message_id,
        from_address: form.from_address,
        subject: form.subject,
        body: form.body,
        ...(receivedAt !== undefined ? { received_at: receivedAt } : {}),
      });

      setNotice({
        tone: "status",
        text: `Stored as inbox item #${item.id} (${item.status}). Re-submitting the same provider message id returns this stored row unchanged — never a duplicate, never an overwrite.`,
      });
      setForm(EMPTY_FORM);
      onFiled?.();
    } catch (e) {
      setNotice({ tone: "alert", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card-surface p-3" data-testid="inbox-composer">
      <p className="ui-label text-ink">File a message</p>
      <p className="mt-1 text-xs text-ink-muted">
        Writes one customer message into the owner-only inbox (POST /support/inbox). Required:
        provider message id, from address, subject, body.
      </p>

      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor={idField} className="ui-label text-ink-muted">
            Provider message id (required)
          </label>
          <input
            id={idField}
            type="text"
            value={form.provider_message_id}
            onChange={(e) => set("provider_message_id")(e.target.value)}
            placeholder="The provider's own message id"
            className={INPUT_CLASS}
          />
        </div>
        <div>
          <label htmlFor={fromField} className="ui-label text-ink-muted">
            From address (required)
          </label>
          <input
            id={fromField}
            type="text"
            value={form.from_address}
            onChange={(e) => set("from_address")(e.target.value)}
            placeholder="customer@example.com"
            className={INPUT_CLASS}
          />
        </div>
        <div className="sm:col-span-2">
          <label htmlFor={subjectField} className="ui-label text-ink-muted">
            Subject (required)
          </label>
          <input
            id={subjectField}
            type="text"
            value={form.subject}
            onChange={(e) => set("subject")(e.target.value)}
            className={INPUT_CLASS}
          />
        </div>
        <div className="sm:col-span-2">
          <label htmlFor={bodyField} className="ui-label text-ink-muted">
            Body (required)
          </label>
          <textarea
            id={bodyField}
            rows={4}
            value={form.body}
            onChange={(e) => set("body")(e.target.value)}
            className={INPUT_CLASS}
          />
        </div>
        <div>
          <label htmlFor={receivedField} className="ui-label text-ink-muted">
            Arrived at (optional)
          </label>
          <input
            id={receivedField}
            type="datetime-local"
            value={form.received_at}
            onChange={(e) => set("received_at")(e.target.value)}
            className={INPUT_CLASS}
          />
          <p className="mt-1 text-xs text-ink-muted">
            Left blank it is not stated (never invented). A stated time is sent as its instant
            with an offset — the server refuses a timezone-less value by name.
          </p>
        </div>
      </div>

      {notice ? (
        <div
          role={notice.tone}
          data-tone={notice.tone}
          className={
            notice.tone === "alert"
              ? "mt-3 border border-alert bg-surface p-3"
              : "mt-3 border border-line-strong bg-surface-sunk p-3"
          }
        >
          <p className="text-sm text-ink">{notice.text}</p>
        </div>
      ) : null}

      <div className="mt-3 flex items-center gap-2 border-t border-line pt-3">
        <p className="ui-label mr-auto text-ink-muted">
          {complete ? "Ready to file." : "Fill the four required fields."}
        </p>
        <Button variant="glossy" disabled={!canSubmit} onClick={submit}>
          {busy ? "Filing…" : "File message"}
        </Button>
      </div>
    </section>
  );
}
