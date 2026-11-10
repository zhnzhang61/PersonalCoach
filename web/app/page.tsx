import { PageHeader } from "@/components/page-header";
import { TodayEyebrow } from "@/components/today-eyebrow";
import { ExternalEvents } from "@/components/health/external-events";
import { ReadinessCard } from "@/components/health/readiness-card";
import { RecoveryChart } from "@/components/health/recovery-chart";
import { SleepChart } from "@/components/health/sleep-chart";
import { SnapshotCards } from "@/components/health/snapshot-cards";
import { TodaysCheckin } from "@/components/health/todays-checkin";

export default function HealthPage() {
  return (
    <div className="mx-auto w-full max-w-4xl lg:max-w-6xl">
      <PageHeader eyebrow={<TodayEyebrow />} title="Health" />
      {/* Phone: one column, in this DOM order. Desktop: what you enter
          (check-in, context) on the left, what the watch says on the
          right, the two 30-day charts side by side underneath. */}
      <div className="grid gap-6 px-5 pb-8 sm:px-8 lg:grid-cols-2 lg:items-start">
        <div className="space-y-6">
          {/* Today's check-in goes ABOVE the objective Garmin cards —
            * subjective state is the first thing the user reflects on,
            * and surfacing the question primes them to answer it. */}
          <TodaysCheckin />
          {/* Context events (travel / illness / life stress) sit below
            * the check-in but above the sensor cards. The agent reads
            * these together with HRV/RHR to decide whether a number
            * means something or is a known degraded-data day. */}
          <ExternalEvents />
        </div>
        <div className="space-y-6">
          <SnapshotCards />
          <ReadinessCard />
        </div>
        <RecoveryChart days={30} />
        <SleepChart days={30} />
      </div>
    </div>
  );
}
