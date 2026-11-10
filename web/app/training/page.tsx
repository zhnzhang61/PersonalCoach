import { Target } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { TodayEyebrow } from "@/components/today-eyebrow";
import { TrainingSelector } from "@/components/training-selector";
import { CoachingTips } from "@/components/training/coaching-tips";
import { CycleOverview } from "@/components/training/cycle-overview";
import { MonthlyChart } from "@/components/training/monthly-chart";
import { PlaceholderCard } from "@/components/training/placeholder-card";
import { PlanCalendar } from "@/components/training/plan-calendar";
import { UpcomingWorkouts } from "@/components/training/upcoming-workouts";

export default function TrainingPage() {
  return (
    <div className="mx-auto w-full max-w-4xl lg:max-w-6xl">
      <PageHeader eyebrow={<TodayEyebrow />} title="Training" />
      {/* Phone: one column in DOM order. Desktop: the cycle overview
          spans the width, then pairs sit side by side — volume chart
          with the calendar, upcoming workouts with the coaching tips. */}
      <div className="grid gap-4 px-5 pb-8 sm:px-8 lg:grid-cols-2 lg:items-start">
        <div className="lg:col-span-2">
          <TrainingSelector />
        </div>
        <div className="lg:col-span-2">
          <CycleOverview />
        </div>
        <MonthlyChart />
        <PlanCalendar />
        <UpcomingWorkouts />
        <CoachingTips />
        <div className="lg:col-span-2">
          <PlaceholderCard
            Icon={Target}
            title="Race time predictor"
            description="Project finish times from completed work in the cycle."
          />
        </div>
      </div>
    </div>
  );
}
