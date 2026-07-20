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
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader eyebrow={<TodayEyebrow />} title="Training" />
      <div className="space-y-4 px-5 pb-8 sm:px-8">
        <TrainingSelector />
        <CycleOverview />
        <MonthlyChart />
        <PlanCalendar />
        <UpcomingWorkouts />
        <CoachingTips />
        <PlaceholderCard
          Icon={Target}
          title="Race time predictor"
          description="Project finish times from completed work in the cycle."
        />
      </div>
    </div>
  );
}
