import { format } from "date-fns";
import { PageHeader } from "@/components/page-header";
import { TrainingSelector } from "@/components/training-selector";
import { WeekBanner } from "@/components/activity/week-banner";
import { RunList } from "@/components/activity/run-list";
import { AddActivityForm } from "@/components/activity/add-activity-form";

export default function ActivityPage() {
  const today = format(new Date(), "EEEE, MMMM d");
  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader eyebrow={today} title="Activity" />
      <div className="space-y-4 px-5 pb-8 sm:px-8">
        <TrainingSelector />
        <WeekBanner />
        <AddActivityForm />
        <RunList />
      </div>
    </div>
  );
}
