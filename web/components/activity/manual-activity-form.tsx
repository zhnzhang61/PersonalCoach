"use client";

import { useState } from "react";
import { Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ManualActivityType } from "@/lib/types";

const TYPES: { value: ManualActivityType; label: string }[] = [
  { value: "run", label: "Run" },
  { value: "swim", label: "Swim" },
  { value: "gym", label: "Gym" },
  { value: "other", label: "Other" },
];

export interface ManualActivityFormValues {
  date: string;
  type: ManualActivityType;
  description: string;
  duration_min: number | null;
  distance_mi: number | null;
}

export interface ManualActivityFormInitial {
  date?: string;
  type?: ManualActivityType;
  description?: string;
  duration_min?: number | null;
  distance_mi?: number | null;
}

interface Props {
  title: string;
  initial?: ManualActivityFormInitial;
  saveLabel?: string;
  pending?: boolean;
  error?: string;
  // Always provided. Called with form values on Save.
  onSubmit: (values: ManualActivityFormValues) => void;
  onCancel: () => void;
  // Optional. When provided, a destructive trash-can button is shown.
  onDelete?: () => void;
  deletePending?: boolean;
}

function localTodayIso(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function ManualActivityForm({
  title,
  initial,
  saveLabel = "Save",
  pending = false,
  error,
  onSubmit,
  onCancel,
  onDelete,
  deletePending = false,
}: Props) {
  const [type, setType] = useState<ManualActivityType>(initial?.type ?? "gym");
  const [date, setDate] = useState(initial?.date ?? localTodayIso());
  const [desc, setDesc] = useState(initial?.description ?? "");
  const [duration, setDuration] = useState(
    initial?.duration_min != null ? String(initial.duration_min) : "",
  );
  const [distance, setDistance] = useState(
    initial?.distance_mi != null ? String(initial.distance_mi) : "",
  );
  const showDistance = type === "run" || type === "swim";

  const submit = () => {
    onSubmit({
      date,
      type,
      description: desc,
      duration_min: duration ? Number(duration) : null,
      distance_mi: distance && showDistance ? Number(distance) : null,
    });
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{title}</h3>
        <button
          type="button"
          onClick={onCancel}
          className="text-muted-foreground hover:text-foreground"
          aria-label="Close"
        >
          <X className="size-4" />
        </button>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {TYPES.map((t) => (
          <button
            key={t.value}
            type="button"
            onClick={() => setType(t.value)}
            className={
              "rounded-md border px-3 py-1.5 text-xs font-medium transition-colors " +
              (type === t.value
                ? "border-foreground bg-foreground text-background"
                : "border-border bg-background text-muted-foreground hover:text-foreground")
            }
          >
            {t.label}
          </button>
        ))}
      </div>

      <label className="flex flex-col gap-1">
        <span className="eyebrow text-[10px]">Date</span>
        <Input
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
        />
      </label>

      <div className="grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-[10px]">Duration (min)</span>
          <Input
            type="number"
            inputMode="decimal"
            placeholder="optional"
            value={duration}
            onChange={(e) => setDuration(e.target.value)}
          />
        </label>
        {showDistance && (
          <label className="flex flex-col gap-1">
            <span className="eyebrow text-[10px]">Distance (mi)</span>
            <Input
              type="number"
              inputMode="decimal"
              placeholder="optional"
              value={distance}
              onChange={(e) => setDistance(e.target.value)}
            />
          </label>
        )}
      </div>

      <label className="flex flex-col gap-1">
        <span className="eyebrow text-[10px]">Description</span>
        <textarea
          className="min-h-[72px] w-full rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40"
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          placeholder="What did you do?"
        />
      </label>

      {error && (
        <p className="text-xs text-rose-700 dark:text-rose-300">{error}</p>
      )}

      <div className="flex items-center gap-2">
        <Button
          className="flex-1"
          disabled={pending || deletePending}
          onClick={submit}
        >
          {pending ? "Saving…" : saveLabel}
        </Button>
        {onDelete && (
          <button
            type="button"
            onClick={onDelete}
            disabled={pending || deletePending}
            className="rounded-md border border-rose-500/30 bg-rose-500/10 p-2 text-rose-700 transition-colors hover:bg-rose-500/20 disabled:opacity-50 dark:text-rose-300"
            aria-label="Delete activity"
          >
            <Trash2 className="size-4" />
          </button>
        )}
      </div>
    </div>
  );
}
