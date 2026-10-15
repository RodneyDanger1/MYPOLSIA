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
import { PhoneCard } from "@/components/dashboard/PhoneCard";
import { PolsiaStatusCard } from "@/components/dashboard/PolsiaStatusCard";
import { SocialWidget } from "@/components/dashboard/SocialWidget";
import { TasksPreview } from "@/components/dashboard/TasksPreview";
import { WebsiteCard } from "@/components/dashboard/WebsiteCard";
import { WidgetSlot, WidgetStack } from "@/components/dashboard/WidgetSlots";

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
 * V12-G1 (the Edit Dashboard closer): the regions mount through `WidgetStack` /
 * `WidgetSlot` (`components/dashboard/WidgetSlots.tsx`), which read the owner's
 * layout from the browser-local store (`dashboardLayout.ts`). Show/hide and
 * reorder happen in that store; a hidden widget is unmounted (it makes no
 * request and cannot show a stale value). Defaults = this exact composition, so
 * nothing moves until the owner edits it.
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
          <WidgetStack
            area="left"
            widgets={{
              status: <PolsiaStatusCard />,
              auto_mode: <AutoModePanel />,
              business: <BusinessPanel />,
              website: <WebsiteCard />,
              // V16/W3: the phone-link card (H-P1 consumer) — mounted through the same layout
              // store as every other left-rail widget (hide/reorder included).
              phone: <PhoneCard />,
            }}
          />
        </aside>

        <section aria-label="Tasks and reports" className="space-y-6">
          <WidgetStack
            area="middle"
            widgets={{
              tasks: <TasksPreview />,
              documents: <DailyReportPanel />,
              media: <MediaCard />,
            }}
          />
        </section>

        <section aria-label="Workspace widgets" className="space-y-6">
          <WidgetStack
            area="right"
            widgets={{
              social: <SocialWidget />,
              outreach: <OutreachWidget />,
              ads: <AdsWidget />,
              caps: <CapsPanel />,
            }}
          />
        </section>

        <WidgetSlot id="activity" label="Activity feed" className="h-[640px]">
          <ActivityFeed />
        </WidgetSlot>

        {/* V8/C3: the chat rail is its own region (client component, mounted the way
            ActivityFeed is). At 2xl it takes the feed column's next row — the live anatomy
            puts the chat under the feed rail; at lg it runs full-width under the grid. */}
        <WidgetSlot
          id="chat"
          label="Chat rail"
          className="h-[640px] lg:col-span-2 2xl:col-span-1 2xl:col-start-4"
        >
          <ChatRail />
        </WidgetSlot>
      </div>
    </div>
  );
}
