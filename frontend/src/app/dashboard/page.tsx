import { ActivityFeed } from "@/components/dashboard/ActivityFeed";
import { AdsWidget } from "@/components/dashboard/AdsWidget";
import { BusinessPanel } from "@/components/dashboard/BusinessPanel";
import { CapsPanel } from "@/components/dashboard/CapsPanel";
import { DailyReportPanel } from "@/components/dashboard/DailyReportPanel";
import { DeepSeekKeyOnboarding } from "@/components/dashboard/DeepSeekKeyOnboarding";
import { OutreachWidget } from "@/components/dashboard/OutreachWidget";
import { PolsiaStatusCard } from "@/components/dashboard/PolsiaStatusCard";
import { SocialWidget } from "@/components/dashboard/SocialWidget";
import { TasksPreview } from "@/components/dashboard/TasksPreview";

/**
 * The dashboard, rebuilt as the live product's four-region composition (agent1 dossier §1.3–1.8):
 *
 *   left rail       Polsia status card + Business mini-panel
 *   middle column   Tasks preview + Daily Report (the Documents slot)
 *   right column    Social / Outreach / Ads widgets + the send-caps counter (B9) (the Twitter /
 *                   Email / Meta Ads slots)
 *   feed rail       ActivityFeed (owned by the realtime lane — imported, never edited here)
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
          <BusinessPanel />
        </aside>

        <section aria-label="Tasks and reports" className="space-y-6">
          <TasksPreview />
          <DailyReportPanel />
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
      </div>
    </div>
  );
}
