import { ActivityFeed } from "@/components/dashboard/ActivityFeed";
import { AdsWidget } from "@/components/dashboard/AdsWidget";
import { AutoModePanel } from "@/components/auto_mode/AutoModePanel";
import { BusinessPanel } from "@/components/dashboard/BusinessPanel";
import { CapsPanel } from "@/components/dashboard/CapsPanel";
import { ChatRail } from "@/components/chat/ChatRail";
import { DailyReportPanel } from "@/components/dashboard/DailyReportPanel";
import { DeepSeekKeyOnboarding } from "@/components/dashboard/DeepSeekKeyOnboarding";
import { MediaCard } from "@/components/dashboard/MediaCard";
import { OutreachWidget } from "@/components/dashboard/OutreachWidget";
import { PolsiaStatusCard } from "@/components/dashboard/PolsiaStatusCard";
import { SocialWidget } from "@/components/dashboard/SocialWidget";
import { TasksPreview } from "@/components/dashboard/TasksPreview";
import { WebsiteCard } from "@/components/dashboard/WebsiteCard";

/**
 * The dashboard, rebuilt as the live product's region composition (agent1 dossier §1.3–1.8):
 *
 *   left rail       Polsia status card + Auto Mode panel + Business mini-panel
 *   middle column   Tasks preview + Daily Report (the Documents slot)
 *   right column    Social / Outreach / Ads widgets + the send-caps counter (B9) (the Twitter /
 *                   Email / Meta Ads slots)
 *   feed rail       ActivityFeed (owned by the realtime lane — imported, never edited here)
 *   chat rail       ChatRail (V8/C3) — OUR chat surface. Mounted as its OWN region beneath the
 *                   feed column at 2xl, the position the measured live anatomy puts the
 *                   composer under the feed rail (ui-gap §1.8); full-width at lg. No existing
 *                   region's content or order was touched.
 *
 * The shell (`src/app/layout.tsx`: canvas, max width, page padding, topbar) is another lane's
 * file; this page only composes the regions inside it.
 *
 * Server component on purpose. The old page was a client component only because it fetched the
 * summary itself, after an earlier server-side fetch had baked BUILD-time KPIs into the prerendered
 * HTML (`api.get` with `catch { return null }`) so the numbers could never change. Now every region
 * owns its own read and its own three states, so this page holds no state, no fetch and no fallback
 * values at all — there is nothing here that can go stale or lie.
 *
 * Failure isolation is structural: a region whose endpoint fails renders its own named alert inside
 * its own card. One broken endpoint cannot blank the page, and no region renders another region's
 * data.
 */
export default function DashboardPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-3xl font-bold text-ink">Dashboard</h1>

      {/* Onboarding: visible only while no DeepSeek key is configured (RD9). */}
      <DeepSeekKeyOnboarding />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2 2xl:grid-cols-[280px_minmax(0,1fr)_320px_340px]">
        <aside aria-label="Workspace status" className="space-y-6">
          <PolsiaStatusCard />
          {/* V7-T14: the Auto Mode controls sit directly under the status card — the real state
              lives in GET/POST/DELETE /api/v1/auto-mode (the card above deliberately does not
              duplicate it; see its comment). */}
          <AutoModePanel />
          <BusinessPanel />
          {/* V8b-D2 (parent mount): the Website card — configured URL + measured health line. */}
          <WebsiteCard />
        </aside>

        <section aria-label="Tasks and reports" className="space-y-6">
          <TasksPreview />
          <DailyReportPanel />
          {/* V8b/L3: the Media slot (live: middle column bottom) — the honest empty-state card, no dead IMAGE/VIDEO buttons. */}
          <MediaCard />
        </section>

        <section aria-label="Workspace widgets" className="space-y-6">
          <SocialWidget />
          <OutreachWidget />
          <AdsWidget />
          <CapsPanel />
        </section>

        <aside aria-label="Activity feed" className="h-[640px]">
          <ActivityFeed />
        </aside>

        {/* V8/C3: the chat rail is its own region (client component, mounted the way
            ActivityFeed is). At 2xl it takes the feed column's next row — the live anatomy
            puts the chat under the feed rail; at lg it runs full-width under the grid. */}
        <aside
          aria-label="Chat rail"
          className="h-[640px] lg:col-span-2 2xl:col-span-1 2xl:col-start-4"
        >
          <ChatRail />
        </aside>
      </div>
    </div>
  );
}
