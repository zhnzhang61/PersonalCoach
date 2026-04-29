"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import type {
  BlocksResponse,
  TrainingWeek,
  WeeksResponse,
} from "@/lib/types";
import { useTrainingSelection } from "@/lib/hooks/use-training-selection";

function pickDefaultWeek(weeks: TrainingWeek[], today: string): TrainingWeek | null {
  if (weeks.length === 0) return null;
  for (const w of weeks) {
    if (w.start <= today && today <= w.end) return w;
  }
  // Today past the block: last week. Today before the block: first week.
  if (today > weeks[weeks.length - 1].end) return weeks[weeks.length - 1];
  return weeks[0];
}

const SELECT_CLASS =
  "w-full appearance-none rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40 disabled:cursor-not-allowed disabled:opacity-50";

export function TrainingSelector() {
  const { blockId, weekLabel, hydrated, setBlockId, setWeekLabel } =
    useTrainingSelection();

  const blocksQuery = useQuery({
    queryKey: ["training", "blocks"],
    queryFn: () => apiGet<BlocksResponse>("/api/training/blocks"),
  });

  const weeksQuery = useQuery({
    queryKey: ["training", "weeks", blockId],
    queryFn: () =>
      apiGet<WeeksResponse>(
        `/api/training/weeks?block_id=${encodeURIComponent(blockId!)}`,
      ),
    enabled: hydrated && !!blockId,
  });

  // Default block: prefer the server's "today's block" if nothing stored.
  useEffect(() => {
    if (!hydrated) return;
    const blocks = blocksQuery.data?.blocks ?? [];
    if (blocks.length === 0) return;
    if (blockId && blocks.some((b) => b.id === blockId)) return;
    const next = blocksQuery.data?.active_block_id ?? blocks[0].id;
    setBlockId(next);
  }, [hydrated, blocksQuery.data, blockId, setBlockId]);

  // Default week: today's week within the current block.
  useEffect(() => {
    if (!hydrated) return;
    const weeks = weeksQuery.data?.weeks ?? [];
    if (weeks.length === 0) return;
    if (weekLabel && weeks.some((w) => w.label === weekLabel)) return;
    const today = new Date().toISOString().slice(0, 10);
    const pick = pickDefaultWeek(weeks, today);
    if (pick) setWeekLabel(pick.label);
  }, [hydrated, weeksQuery.data, weekLabel, setWeekLabel]);

  const blocks = blocksQuery.data?.blocks ?? [];
  const weeks = weeksQuery.data?.weeks ?? [];

  if (blocksQuery.isLoading) {
    return (
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="h-10 w-full animate-pulse rounded-md bg-muted/50 sm:flex-1" />
        <div className="h-10 w-full animate-pulse rounded-md bg-muted/50 sm:flex-1" />
      </div>
    );
  }

  if (blocks.length === 0) {
    return (
      <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
        No training blocks yet. Create one in <strong>Setup</strong> to get
        started.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 sm:flex-row">
      <label className="flex flex-col gap-1 sm:flex-1">
        <span className="eyebrow text-[10px]">Cycle</span>
        <select
          className={SELECT_CLASS}
          value={blockId ?? ""}
          onChange={(e) => setBlockId(e.target.value)}
          aria-label="Training cycle"
        >
          {blocks.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name} · {b.start_date} → {b.end_date}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 sm:flex-1">
        <span className="eyebrow text-[10px]">Week</span>
        <select
          className={SELECT_CLASS}
          value={weekLabel ?? ""}
          onChange={(e) => setWeekLabel(e.target.value)}
          disabled={weeks.length === 0}
          aria-label="Training week"
        >
          {weeks.map((w) => (
            <option key={w.label} value={w.label}>
              {w.label}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
