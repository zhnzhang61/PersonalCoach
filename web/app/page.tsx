import { format } from "date-fns";
import { PageHeader } from "@/components/page-header";
import { ReadinessCard } from "@/components/health/readiness-card";
import { RecoveryChart } from "@/components/health/recovery-chart";
import { SleepChart } from "@/components/health/sleep-chart";
import { SnapshotCards } from "@/components/health/snapshot-cards";

export default function HealthPage() {
  const today = format(new Date(), "EEEE, MMMM d");
  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader eyebrow={today} title="Health" />
      <div className="space-y-6 px-5 pb-8 sm:px-8">
        <SnapshotCards />
        <ReadinessCard />
        <RecoveryChart days={30} />
        <SleepChart days={30} />
      </div>
    </div>
  );
}
