"use client";

import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui";
import { api } from "@/lib/api";

import { SCRIM } from "./TaskDetailSheet";
import { errorText } from "./status";
import type { TaskRow } from "./types";

/**
 * The New Task dialog (wave V11 unit 2) — the missing half of `POST /api/v1/tasks`
 * (`backend/app/api/v1/tasks.py:86-110`), per the wiring audit's F7.
 *
 * The form is the REAL create schema (`schemas/task.py::TaskCreate`), field for field:
 *   * `title` — required, 1..512 (pydantic enforces; the client maxLength states the same
 *     bound so a 513th character is not typed instead of refused);
 *   * `agent_type` — the nine values of `task_service.VALID_AGENT_TYPES` (an unknown one is
 *     refused by name, E_AGENT_UNKNOWN 422 — the refusal still renders even though this
 *     select cannot produce one);
 *   * `description` — optional; sent ONLY when non-blank, so a blank field stores NULL, never
 *     an empty string;
 *   * `priority` — integer, schema default 3; blank → the key is omitted and the schema's own
 *     default applies (this build adds no bounds the backend does not have);
 *   * `source` / `scheduled_date` — deliberately NOT collected (no operator-meaningful choice
 *     exists yet; omitting them applies the schema defaults `source="dashboard"`,
 *     `scheduled_date=None`, i.e. the task is claimable now).
 *
 * Shell conventions are the sheet's (sheets-and-composer.md §1): the same SCRIM token, a white
 * panel with `role="dialog"` + `aria-modal`, focus moving in and back out, Esc to close, and
 * no scrim-click dismissal (OPEN DECISION #2). Refusals render `ApiError.display`; the created
 * task is reported from the SERVER's response (`created.id`), never assumed.
 */

/** `task_service.VALID_AGENT_TYPES` (`backend/app/services/task_service.py:52-62`), in source
 *  order. Duplicated as data (no generated client); the u11 suite pins the list. */
export const VALID_AGENT_TYPES = [
  "orchestrator",
  "business_planning",
  "competitor_research",
  "social_media",
  "ads_management",
  "email_outreach",
  "code_generation",
  "customer_support",
  "finance",
] as const;

type Notice = { tone: "status" | "alert"; text: string };

const INPUT_CLASS = "w-full border border-line-strong bg-surface px-3 py-2 text-sm text-ink";

export function NewTaskDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  /** Called after a 201 with the server's returned task (the board refetches on it). */
  onCreated?: (created: TaskRow) => void;
}) {
  const titleId = useId();
  const titleFieldId = useId();
  const agentFieldId = useId();
  const descriptionFieldId = useId();
  const priorityFieldId = useId();
  const priorityErrorId = useId();
  const titleInputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const [title, setTitle] = useState("");
  const [agentType, setAgentType] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("3");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    titleInputRef.current?.focus();
    return () => {
      openerRef.current?.focus();
    };
  }, []);

  const priorityTrimmed = priority.trim();
  const priorityValid = priorityTrimmed === "" || /^-?\d+$/.test(priorityTrimmed);
  // No dead button: submit arms only when the two required fields are filled and the optional
  // priority (if typed at all) is an integer.
  const canSubmit = !busy && title.trim() !== "" && agentType !== "" && priorityValid;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setNotice(null);
    try {
      const created = await api.post<TaskRow>("/tasks", {
        title,
        agent_type: agentType,
        ...(description.trim() !== "" ? { description } : {}),
        ...(priorityTrimmed === "" ? {} : { priority: Number(priorityTrimmed) }),
      });
      setNotice({
        tone: "status",
        text: `Task #${created.id} created — status "${created.status ?? "unknown"}" (the server's response). The row now exists in the queue.`,
      });
      onCreated?.(created);
    } catch (e) {
      // Refused by name (E_AGENT_UNKNOWN / E_VALIDATION / E_DB …) — rendered as the server
      // spelled it; nothing was created, so nothing on the board moved.
      setNotice({ tone: "alert", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4"
      style={{ backgroundColor: SCRIM }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            onClose();
            return;
          }
          // Tab is trapped inside the modal: focus must not walk into the page behind it.
          if (e.key !== "Tab") return;
          const panel = dialogRef.current;
          if (!panel) return;
          const focusables = Array.from(
            panel.querySelectorAll<HTMLElement>(
              'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
            ),
          ).filter((element) => !element.hasAttribute("disabled") && element.tabIndex >= 0);
          if (focusables.length === 0) return;
          const first = focusables[0];
          const last = focusables[focusables.length - 1];
          const active = document.activeElement;
          if (e.shiftKey) {
            if (active === first || !panel.contains(active)) {
              e.preventDefault();
              last.focus();
            }
          } else if (active === last || !panel.contains(active)) {
            e.preventDefault();
            first.focus();
          }
        }}
        className="card-surface my-4 w-full max-w-[560px] shadow-2xl"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-2">
          <h2 id={titleId} className="ui-label text-ink">
            New task
          </h2>
          <Button size="sm" aria-label="Close new task dialog" className="ml-auto" onClick={onClose}>
            ✕
          </Button>
        </header>

        <div className="max-h-[70vh] overflow-y-auto px-4 py-3">
          <div className="space-y-3">
            <div>
              <label htmlFor={titleFieldId} className="ui-label text-ink-muted">
                Title (required)
              </label>
              <input
                ref={titleInputRef}
                id={titleFieldId}
                type="text"
                aria-required="true"
                maxLength={512}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="What should the agent do?"
                className={INPUT_CLASS}
              />
            </div>

            <div>
              <label htmlFor={agentFieldId} className="ui-label text-ink-muted">
                Agent (required)
              </label>
              <select
                id={agentFieldId}
                aria-required="true"
                value={agentType}
                onChange={(e) => setAgentType(e.target.value)}
                className={INPUT_CLASS}
              >
                <option value="">Choose an agent…</option>
                {VALID_AGENT_TYPES.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor={descriptionFieldId} className="ui-label text-ink-muted">
                Description (optional)
              </label>
              <textarea
                id={descriptionFieldId}
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Context the agent needs. Left blank, it is stored as not stated."
                className={INPUT_CLASS}
              />
            </div>

            <div>
              <label htmlFor={priorityFieldId} className="ui-label text-ink-muted">
                Priority (integer, default 3)
              </label>
              <input
                id={priorityFieldId}
                type="number"
                step={1}
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
                aria-invalid={priorityValid ? undefined : true}
                aria-describedby={priorityValid ? undefined : priorityErrorId}
                className={INPUT_CLASS}
              />
              {priorityValid ? null : (
                <p id={priorityErrorId} role="alert" className="mt-1 text-xs text-alert">
                  Priority must be a whole number — clear it to use the server&rsquo;s default (3).
                </p>
              )}
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

          <div className="mt-4 flex items-center gap-2 border-t border-line pt-3">
            <p className="ui-label mr-auto text-ink-muted">
              POST /tasks — the task is created immediately in the pending queue.
            </p>
            <Button variant="glossy" disabled={!canSubmit} onClick={submit}>
              {busy ? "Creating…" : "Create task"}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
