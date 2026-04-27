import { PageHeader } from "@/components/page-header";
import { TodayCards } from "@/components/health/today-cards";
import { TimelineChart } from "@/components/health/timeline-chart";

export default function HealthPage() {
  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader title="Health" subtitle="Today at a glance" />
      <div className="space-y-4 px-5 pb-8 sm:px-8">
        <TodayCards />
        <TimelineChart days={30} />
      </div>
    </div>
  );
}
