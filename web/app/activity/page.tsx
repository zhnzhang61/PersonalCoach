import { PageHeader } from "@/components/page-header";
import { TodayEyebrow } from "@/components/today-eyebrow";
import { TrainingSelector } from "@/components/training-selector";
import { WeekBanner } from "@/components/activity/week-banner";
import { RunList } from "@/components/activity/run-list";
import { AddActivityForm } from "@/components/activity/add-activity-form";

export default function ActivityPage() {
  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader eyebrow={<TodayEyebrow />} title="Activity" />
      <div className="space-y-4 px-5 pb-8 sm:px-8">
        <TrainingSelector />
        <WeekBanner />
        <AddActivityForm />
        <RunList />
      </div>
    </div>
  );
}
