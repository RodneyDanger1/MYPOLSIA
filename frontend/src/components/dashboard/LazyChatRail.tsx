"use client";

import dynamic from "next/dynamic";

/**
 * V24/L5 — the dashboard's chat rail, loaded on demand.
 *
 * WHY: the dashboard's first paint does not need the chat transcript/composer code. It is the
 * bottom region of a 640 px slot (below the fold at every measured viewport), yet a static import
 * put `ChatRail` + `useChat` (≈13 KB of source) into `/dashboard`'s first-load JS, competing with
 * the widgets the owner actually sees. This wrapper moves it into its own chunk.
 *
 * `ssr: false` is deliberate: the rail is a client-only surface (its socket/history read only
 * exists in the browser), so rendering it server-side buys nothing. The fallback reserves the
 * region's box so the page cannot reflow while the chunk arrives.
 */
const ChatRail = dynamic(
  () => import("@/components/chat/ChatRail").then((mod) => mod.ChatRail),
  {
    ssr: false,
    loading: () => (
      <div
        className="card-surface h-full min-h-[320px] animate-pulse"
        data-testid="chat-rail-loading"
        aria-busy="true"
        aria-label="Chat rail loading"
      />
    ),
  },
);

export function LazyChatRail() {
  return <ChatRail />;
}
