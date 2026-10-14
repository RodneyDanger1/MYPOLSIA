"use client";

import { useState } from "react";
import { Composer } from "@/components/chat/Composer";
import { ConfirmCard } from "@/components/chat/ConfirmCard";
import { useChat, type ChatItem, type ChatRefusal } from "@/components/chat/useChat";
import { absoluteStamp, relativeTime } from "@/components/dashboard/format";
import { PanelError, PanelSkeleton } from "@/components/dashboard/states";
import { Button } from "@/components/ui";

/**
 * The chat rail: OUR conversational surface, mounted as its own dashboard region beneath the
 * feed column (the position the measured live anatomy puts the composer — ui-gap §1.8). Its
 * row grammar follows ActivityFeed (white card, 1px hairline, mono-caps kicker, serif body,
 * mono timestamp); every line of copy is ours and only rendered when the data backs it.
 *
 * The transcript renders what the wire sent: owner right / polsia left, `relativeTime` visible
 * with `absoluteStamp` in the `title`, and the shared honest placeholders (a null timestamp is
 * "date unknown", never "Invalid Date"). A refusal from the 201 body renders the
 * `ApiError.display` convention — code + message + fix hint. Everything else (skeleton, named
 * failure + RETRY, the empty sentence) reuses the dashboard's shared state vocabulary.
 */

/** The ApiError.display convention, for a refusal that arrives inside a 201 success body —
 *  there is no HTTP failure to build an ApiError from, so the same code + message + fix shape
 *  is rendered from the envelope itself. */
function refusalDisplay(refusal: ChatRefusal): string {
  return refusal.how_to_fix
    ? `${refusal.code}: ${refusal.message} — ${refusal.how_to_fix}`
    : `${refusal.code}: ${refusal.message}`;
}

const ROLE_LABEL: Record<string, string> = { owner: "You", polsia: "Polsia" };

function MessageRow({ item }: { item: ChatItem }) {
  const isOwner = item.role === "owner";
  return (
    <li
      data-role={item.role}
      className={`w-fit max-w-[85%] rounded-sm border border-[#e3e3e3] px-3 py-2.5 ${
        isOwner ? "ml-auto bg-[#FAFAFA]" : "bg-white"
      }`}
    >
      <span className="font-mono text-[10px] uppercase tracking-wider text-[#6e6e6e]">
        {ROLE_LABEL[item.role] ?? item.role.replace(/_/g, " ")}
      </span>
      {item.content ? (
        <p className="mt-1 whitespace-pre-wrap font-serif text-sm leading-snug text-[#111]">
          {item.content}
        </p>
      ) : null}
      <div className="mt-1.5 flex items-center gap-2">
        <time
          className="font-mono text-[10px] text-[#8b8b8b]"
          dateTime={item.created_at ?? undefined}
          title={absoluteStamp(item.created_at)}
        >
          {relativeTime(item.created_at)}
        </time>
        {item.task_id !== null ? (
          <span className="font-mono text-[10px] uppercase tracking-wider text-[#6e6e6e]">
            Task #{item.task_id}
          </span>
        ) : null}
      </div>
      {item.refusal ? (
        <p className="mt-2 rounded-sm border border-red-200 bg-red-50 px-2 py-1.5 font-mono text-[10px] leading-relaxed text-red-800">
          {refusalDisplay(item.refusal)}
        </p>
      ) : null}
    </li>
  );
}

export function ChatRail() {
  const chat = useChat();
  const [draft, setDraft] = useState("");

  const submit = async () => {
    const message = draft.trim();
    if (chat.sending || message === "") return;
    try {
      await chat.send(message);
      setDraft(""); // the message made it — only now is the composer cleared
    } catch {
      // chat.send already recorded the named failure; the draft stays for another try.
    }
  };

  const gate = chat.confirm;
  const showEmpty = chat.status === "ready" && chat.items.length === 0;

  return (
    <section
      aria-label="Chat"
      data-testid="chat-rail"
      className="flex h-full flex-col rounded-sm border border-[#E3E3E3] bg-white"
    >
      <div className="flex items-center justify-between border-b border-[#E3E3E3] px-3 py-2.5">
        <h2 className="font-serif text-base text-[#111]">Chat</h2>
        {chat.status === "ready" && chat.total !== null && chat.total > 0 ? (
          <span
            data-testid="chat-count"
            className="font-mono text-[10px] uppercase tracking-wider text-[#6e6e6e]"
          >
            {chat.items.length === chat.total
              ? `${chat.total} ${chat.total === 1 ? "MESSAGE" : "MESSAGES"}`
              : `SHOWING ${chat.items.length} OF ${chat.total}`}
          </span>
        ) : null}
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2">
        {chat.status === "loading" && <PanelSkeleton rows={4} />}

        {chat.status === "error" && (
          <div className="space-y-2">
            <PanelError label="chat history" message={chat.error ?? "no reason supplied"} />
            <Button size="sm" onClick={() => void chat.load()}>
              RETRY
            </Button>
          </div>
        )}

        {showEmpty && (
          <p className="font-serif text-xs leading-relaxed text-[#6e6e6e]">
            No messages yet. Ask about your tasks or spend, or ask to file a new task — a task is
            created only after you confirm it.
          </p>
        )}

        {chat.items.length > 0 && (
          <ul className="space-y-2 pb-2">
            {chat.items.map((item, index) => (
              <MessageRow key={`${item.role}-${index}`} item={item} />
            ))}
          </ul>
        )}

        {gate ? (
          <ConfirmCard gate={gate} onConfirm={() => void chat.confirmAction(gate.token)} />
        ) : null}

        {chat.sendError ? (
          <p
            role="alert"
            className="mt-2 rounded-sm border border-red-200 bg-red-50 px-2 py-1.5 font-mono text-[10px] leading-relaxed text-red-800"
          >
            Could not send: {chat.sendError}
          </p>
        ) : null}
      </div>

      <Composer
        value={draft}
        onChange={setDraft}
        onSubmit={() => void submit()}
        sending={chat.sending}
      />
    </section>
  );
}
