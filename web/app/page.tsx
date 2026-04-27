import { format } from "date-fns";
import { PageHeader } from "@/components/page-header";
import { TodayCards } from "@/components/health/today-cards";
import { TimelineChart } from "@/components/health/timeline-chart";

export default function HealthPage() {
  const today = format(new Date(), "EEEE, MMMM d");
  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader eyebrow={today} title="Health" />
      <div className="space-y-6 px-5 pb-8 sm:px-8">
        <TodayCards />
        <TimelineChart days={30} />
      </div>
    </div>
  );
}
