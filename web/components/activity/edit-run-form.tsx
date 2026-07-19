"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check } from "lucide-react";
import { apiGet, apiPut } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import type {
  LapsResponse,
  LapsUpdateBody,
  RunActivity,
} from "@/lib/types";
import { EffortPaintEditor } from "@/components/activity/effort-paint-editor";

interface Props {
  run: RunActivity;
  onClose: () => void;
}

export function EditRunForm({ run, onClose }: Props) {
  const qc = useQueryClient();
  const lapsQuery = useQuery({
    queryKey: ["runs", run.activityId, "laps"],
    queryFn: () =>
      apiGet<LapsResponse>(`/api/runs/${run.activityId}/laps`),
  });

  const [name, setName] = useState(
    run.manual_meta?.name || run.activityName || "Run",
  );
  const [notes, setNotes] = useState(run.manual_meta?.notes ?? "");
  // The server's categories are the baseline; per-lap overrides layer on top
  // until save. This avoids setState-in-effect — no hydration step needed.
  const [overrides, setOverrides] = useState<Record<number, string>>({});

  const mutation = useMutation({
    mutationFn: (body: LapsUpdateBody) =>
      apiPut<{ ok: boolean; activity_id: number }>(
        `/api/runs/${run.activityId}/laps`,
        body as unknown as Record<string, unknown>,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["runs"] });
      qc.invalidateQueries({
        queryKey: ["runs", run.activityId, "laps"],
      });
      qc.invalidateQueries({ queryKey: ["training", "cycle-stats"] });
      onClose();
    },
  });

  const laps = lapsQuery.data?.laps ?? [];
  const categoryAt = (i: number): string =>
    overrides[i] ?? laps[i]?.category ?? "Hold Back Easy";

  const paintLaps = (indices: number[], category: string) => {
    setOverrides((prev) => {
      const next = { ...prev };
      indices.forEach((i) => {
        next[i] = category;
      });
      return next;
    });
  };

  const setAllCategories = (categories: string[]) => {
    setOverrides(Object.fromEntries(categories.map((c, i) => [i, c])));
  };

  const onSave = () => {
    if (laps.length === 0) return;
    const finalCategories = laps.map((_, i) => categoryAt(i));
    mutation.mutate({
      week_num: run.manual_meta?.week_num ?? 0,
      run_name: name,
      categories: finalCategories,
      notes,
    });
  };

  // Embedded inside RunCard now (no outer Card wrapper). Caller is
  // responsible for visual chrome / Separator above this block.
  return (
    <div className="space-y-4">
      <label className="flex flex-col gap-1">
        <span className="eyebrow text-xs">Name</span>
        <Input
          className="text-base"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="eyebrow text-xs">Notes</span>
        <textarea
          className="min-h-[80px] w-full rounded-md border border-border bg-background px-3 py-2 text-base shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Subjective notes — how it felt, aches, pacing thoughts."
        />
      </label>

      {lapsQuery.isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : laps.length === 0 ? (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300">
          No lap data found for this run. (Try syncing splits in Setup.)
        </div>
      ) : (
        <div className="rounded-md border border-border bg-muted/10 p-3">
          <EffortPaintEditor
            run={run}
            laps={laps}
            categories={laps.map((_, i) => categoryAt(i))}
            onPaint={paintLaps}
            onSetAll={setAllCategories}
          />
        </div>
      )}

      {mutation.isError && (
        <p className="text-sm text-rose-700 dark:text-rose-300">
          Save failed: {(mutation.error as Error).message}
        </p>
      )}

      <div className="flex gap-2">
        <Button
          className="flex-1 gap-1.5"
          onClick={onSave}
          disabled={
            mutation.isPending || lapsQuery.isLoading || laps.length === 0
          }
        >
          <Check className="size-4" />
          {mutation.isPending ? "Saving…" : "Save"}
        </Button>
        <Button
          variant="outline"
          onClick={onClose}
          disabled={mutation.isPending}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
