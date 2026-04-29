"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";
import { apiPost } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import type { ManualActivity, ManualActivityType } from "@/lib/types";

const TYPES: { value: ManualActivityType; label: string }[] = [
  { value: "run", label: "Run" },
  { value: "swim", label: "Swim" },
  { value: "gym", label: "Gym" },
  { value: "other", label: "Other" },
];

export function AddActivityForm() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const today = (() => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  })();
  const [type, setType] = useState<ManualActivityType>("gym");
  const [date, setDate] = useState(today);
  const [desc, setDesc] = useState("");
  const [duration, setDuration] = useState("");
  const [distance, setDistance] = useState("");

  const mutation = useMutation({
    mutationFn: (payload: {
      date: string;
      type: ManualActivityType;
      description: string;
      duration_min: number | null;
      distance_mi: number | null;
    }) =>
      apiPost<{ ok: boolean; activity: ManualActivity }>(
        "/api/manual-activities",
        payload,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["manual-activities"] });
      setOpen(false);
      setDesc("");
      setDuration("");
      setDistance("");
    },
  });

  if (!open) {
    return (
      <Button
        variant="outline"
        className="w-full justify-center gap-2"
        onClick={() => setOpen(true)}
      >
        <Plus className="size-4" aria-hidden />
        Add activity (swim / gym / manual run)
      </Button>
    );
  }

  const showDistance = type === "run" || type === "swim";

  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">New activity</h3>
          <button
            type="button"
            onClick={() => setOpen(false)}
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

        {mutation.isError && (
          <p className="text-xs text-rose-700 dark:text-rose-300">
            Failed to save: {(mutation.error as Error).message}
          </p>
        )}

        <Button
          className="w-full"
          disabled={mutation.isPending}
          onClick={() =>
            mutation.mutate({
              date,
              type,
              description: desc,
              duration_min: duration ? Number(duration) : null,
              distance_mi: distance && showDistance ? Number(distance) : null,
            })
          }
        >
          {mutation.isPending ? "Saving…" : "Save"}
        </Button>
      </CardContent>
    </Card>
  );
}
