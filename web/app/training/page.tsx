import { Calendar, History, Sparkles, Target } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { TrainingSelector } from "@/components/training-selector";
import { CycleOverview } from "@/components/training/cycle-overview";
import { PlaceholderCard } from "@/components/training/placeholder-card";

export default function TrainingPage() {
  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader title="Training" />
      <div className="space-y-4 px-5 pb-8 sm:px-8">
        <TrainingSelector />
        <CycleOverview />
        <PlaceholderCard
          Icon={History}
          title="Historical stats"
          description="Compare cycles side by side — total mileage, peak weeks, effort mix."
        />
        <PlaceholderCard
          Icon={Calendar}
          title="Plan calendar"
          description="Schedule workouts across the cycle and check off as you go."
        />
        <PlaceholderCard
          Icon={Sparkles}
          title="AI training plans"
          description="Generate a few candidate plans from your goal and recent load."
        />
        <PlaceholderCard
          Icon={Target}
          title="Race time predictor"
          description="Project finish times from completed work in the cycle."
        />
      </div>
    </div>
  );
}
