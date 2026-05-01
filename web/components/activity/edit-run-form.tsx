"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check } from "lucide-react";
import { apiGet, apiPut } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  EFFORT_CATEGORIES,
  type Lap,
  type LapsResponse,
  type LapsUpdateBody,
  type RunActivity,
} from "@/lib/types";

interface Props {
  run: RunActivity;
  onClose: () => void;
}

// Bumped from text-xs / py-1 to feel tappable on a phone.
const SELECT_CLASS =
  "rounded-md border border-border bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40";

function metersToMi(m: number): number {
  return m / 1609.34;
}

function paceForLap(lap: Lap): string {
  if (lap.distance <= 0 || lap.duration <= 0) return "—";
  const dec = lap.duration / 60 / metersToMi(lap.distance);
  return `${Math.floor(dec)}:${Math.floor((dec % 1) * 60)
    .toString()
    .padStart(2, "0")}`;
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
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [bulkCat, setBulkCat] = useState<string>("Hold Back Easy");

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

  const updateLapCategory = (i: number, c: string) => {
    setOverrides((prev) => ({ ...prev, [i]: c }));
  };

  const toggleSelected = (i: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  };

  const applyBulk = () => {
    if (selected.size === 0) return;
    setOverrides((prev) => {
      const next = { ...prev };
      selected.forEach((i) => {
        next[i] = bulkCat;
      });
      return next;
    });
    setSelected(new Set());
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

  const allChecked = useMemo(
    () => laps.length > 0 && selected.size === laps.length,
    [laps.length, selected.size],
  );

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
        <>
          <div className="space-y-2 rounded-md border border-border bg-muted/20 p-3">
            <p className="eyebrow text-xs">Bulk edit</p>
            <div className="flex flex-wrap items-center gap-2">
              <select
                className={SELECT_CLASS}
                value={bulkCat}
                onChange={(e) => setBulkCat(e.target.value)}
                aria-label="Bulk category"
              >
                {EFFORT_CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
              <Button
                variant="outline"
                size="sm"
                onClick={applyBulk}
                disabled={selected.size === 0}
              >
                Apply to {selected.size || "selected"}
              </Button>
              <button
                type="button"
                className="text-sm text-muted-foreground underline"
                onClick={() =>
                  setSelected(
                    allChecked ? new Set() : new Set(laps.map((_, i) => i)),
                  )
                }
              >
                {allChecked ? "Clear" : "Select all"}
              </button>
            </div>
          </div>

          <div className="overflow-hidden rounded-md border border-border">
            <table className="w-full text-left text-sm">
              <thead className="bg-muted/40 text-muted-foreground">
                <tr>
                  <th className="w-8 py-2 pl-2"></th>
                  <th className="py-2 pr-2 font-medium">Lap</th>
                  <th className="py-2 pr-2 text-right font-medium">Mi</th>
                  <th className="py-2 pr-2 text-right font-medium">Pace</th>
                  <th className="py-2 pr-2 text-right font-medium">HR</th>
                  <th className="py-2 pr-2 font-medium">Effort</th>
                </tr>
              </thead>
              <tbody>
                {laps.map((lap, i) => (
                  <tr key={i} className="border-t border-border/50">
                    <td className="py-2 pl-2">
                      <input
                        type="checkbox"
                        className="size-4"
                        checked={selected.has(i)}
                        onChange={() => toggleSelected(i)}
                        aria-label={`Select lap ${i + 1}`}
                      />
                    </td>
                    <td className="py-2 pr-2 tabular-nums">{i + 1}</td>
                    <td className="py-2 pr-2 text-right tabular-nums">
                      {metersToMi(lap.distance).toFixed(2)}
                    </td>
                    <td className="py-2 pr-2 text-right tabular-nums">
                      {paceForLap(lap)}
                    </td>
                    <td className="py-2 pr-2 text-right tabular-nums">
                      {lap.averageHR ?? "—"}
                    </td>
                    <td className="py-2 pr-2">
                      <select
                        className={SELECT_CLASS}
                        value={categoryAt(i)}
                        onChange={(e) =>
                          updateLapCategory(i, e.target.value)
                        }
                        aria-label={`Lap ${i + 1} effort`}
                      >
                        {EFFORT_CATEGORIES.map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
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
