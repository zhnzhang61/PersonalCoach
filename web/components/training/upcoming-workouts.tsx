"use client";

import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Sparkles } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import type {
  PlannedWorkout,
  PlannedWorkoutsResponse,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { EditWorkoutModal } from "./edit-workout-modal";

// Upcoming planned workouts card (PR P4b — replaces the v0
// `PlaceholderCard "AI training plans"`).
//
// Lists the next 14 days of `planned_workouts.json` rows. Two entry
// points to the modal:
//   • Tapping a row → edit mode (modal pre-filled with that row).
//   • "+ Add" button in the header → create mode (modal with today's
//     date prefilled, blank everything else).
//
// Edits and deletes flow through PUT / DELETE `/api/planned-workouts/...`
// which sync to Google Cal automatically when the row has a
// `cal_event_id`. We don't manage that here — backend owns the Cal
// side and reports back via `cal_synced` (we just refetch on success).
//
// AI-authored workouts and manually-added ones look identical; the
// only difference is whether `cal_event_id` is set (we don't surface
// that to the user — it's plumbing).

function todayLocal(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function plusDays(iso: string, days: number): string {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function UpcomingWorkouts() {
  const today = todayLocal();
  const end = plusDays(today, 14);

  const query = useQuery({
    queryKey: ["planned-workouts", today, end],
    queryFn: () =>
      apiGet<PlannedWorkoutsResponse>(
        `/api/planned-workouts?start=${today}&end=${end}`,
      ),
    staleTime: 30_000,
  });

  // `editing` = the row being edited, or "new" for create mode, or
  // null when the modal is closed. Single state slot keeps the modal
  // mount/unmount logic simple and means we can't accidentally have
  // both "editing X" and "creating new" open at the same time.
  const [editing, setEditing] = useState<PlannedWorkout | "new" | null>(null);
  const qc = useQueryClient();

  const sorted = useMemo(() => {
    const items = query.data?.planned_workouts ?? [];
    // API returns ASC date already; we re-sort here so we can rely
    // on the order without trusting the network round-trip.
    return [...items].sort((a, b) => a.date.localeCompare(b.date));
  }, [query.data]);

  return (
    <>
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-2 space-y-0">
          <div>
            <div className="flex items-center gap-2">
              <Sparkles className="size-4 text-warm-accent" />
              <CardTitle className="text-base">AI training plans</CardTitle>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              Next 14 days — tap a row to edit or delete.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setEditing("new")}
            className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border bg-background px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted/40"
          >
            <Plus className="size-3" />
            Add
          </button>
        </CardHeader>
        <CardContent>
          {query.isLoading && !query.data ? (
            <Skeleton className="h-24 w-full" />
          ) : sorted.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">
              No planned workouts in the next 2 weeks. Ask the coach to
              draft one, or tap <span className="font-medium">Add</span> to
              schedule manually.
            </p>
          ) : (
            <ul className="space-y-2">
              {sorted.map((w) => (
                <li key={w.id}>
                  <WorkoutRow
                    workout={w}
                    today={today}
                    onClick={() => setEditing(w)}
                  />
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {editing !== null && (
        <EditWorkoutModal
          mode={editing === "new" ? "create" : "edit"}
          initial={editing === "new" ? null : editing}
          defaultDate={today}
          onClose={() => setEditing(null)}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["planned-workouts"] });
            // Also bust the calendar events cache — the new/edited
            // workout shows up there too once Google Cal syncs.
            qc.invalidateQueries({ queryKey: ["calendar", "events"] });
            setEditing(null);
          }}
        />
      )}
    </>
  );
}

interface WorkoutRowProps {
  workout: PlannedWorkout;
  today: string;
  onClick: () => void;
}

function WorkoutRow({ workout, today, onClick }: WorkoutRowProps) {
  const isToday = workout.date === today;
  const isPast = workout.date < today;

  // Compact metadata chips — only render what was actually set on
  // the plan. A plan with just `{date, type}` shows no chips, which
  // is fine: the row still reads "Easy · Mon May 27".
  const chips: string[] = [];
  if (workout.distance_mi != null) chips.push(`${workout.distance_mi} mi`);
  if (workout.duration_min != null) chips.push(`${workout.duration_min} min`);
  if (workout.target_pace_min_mi != null)
    chips.push(`@ ${formatPace(workout.target_pace_min_mi)}`);
  if (workout.target_hr != null) chips.push(`HR ${workout.target_hr}`);

  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full items-start gap-3 rounded-md border border-border bg-background p-3 text-left transition-colors hover:bg-muted/30",
        isPast && "opacity-60",
      )}
    >
      <div className="flex w-14 shrink-0 flex-col items-center rounded bg-muted/40 py-1.5">
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
          {formatDayLabel(workout.date)}
        </div>
        <div className="text-base font-semibold leading-tight">
          {formatDayNumber(workout.date)}
        </div>
      </div>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-sm font-medium capitalize">{workout.type}</span>
          {isToday && (
            <span className="rounded-full bg-warm-accent/20 px-1.5 py-0.5 text-[10px] font-medium text-warm-accent">
              today
            </span>
          )}
        </div>
        {chips.length > 0 && (
          <div className="flex flex-wrap gap-1.5 text-[11px] text-muted-foreground">
            {chips.map((c) => (
              <span key={c}>{c}</span>
            ))}
          </div>
        )}
        {workout.notes && (
          <p className="line-clamp-2 text-xs text-muted-foreground">
            {workout.notes}
          </p>
        )}
      </div>
    </button>
  );
}

function formatPace(minPerMi: number): string {
  const m = Math.floor(minPerMi);
  const s = Math.round((minPerMi - m) * 60);
  return `${m}:${String(s).padStart(2, "0")}/mi`;
}

function formatDayLabel(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-US", { weekday: "short" });
}

function formatDayNumber(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  return String(d.getDate());
}
