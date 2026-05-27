import { format } from "date-fns";
import { PageHeader } from "@/components/page-header";
import { ExternalEvents } from "@/components/health/external-events";
import { ReadinessCard } from "@/components/health/readiness-card";
import { RecoveryChart } from "@/components/health/recovery-chart";
import { SleepChart } from "@/components/health/sleep-chart";
import { SnapshotCards } from "@/components/health/snapshot-cards";
import { TodaysCheckin } from "@/components/health/todays-checkin";

export default function HealthPage() {
  const today = format(new Date(), "EEEE, MMMM d");
  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader eyebrow={today} title="Health" />
      <div className="space-y-6 px-5 pb-8 sm:px-8">
        {/* Today's check-in goes ABOVE the objective Garmin cards —
          * subjective state is the first thing the user reflects on,
          * and surfacing the question primes them to answer it. */}
        <TodaysCheckin />
        {/* Context events (travel / illness / life stress) sit below
          * the check-in but above the sensor cards. The agent reads
          * these together with HRV/RHR to decide whether a number
          * means something or is a known degraded-data day. */}
        <ExternalEvents />
        <SnapshotCards />
        <ReadinessCard />
        <RecoveryChart days={30} />
        <SleepChart days={30} />
      </div>
    </div>
  );
}
