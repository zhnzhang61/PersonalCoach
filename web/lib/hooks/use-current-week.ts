"use client";

import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import type { TrainingWeek, WeeksResponse } from "@/lib/types";
import { useTrainingSelection } from "@/lib/hooks/use-training-selection";

export interface ResolvedWeek {
  blockId: string | null;
  week: TrainingWeek | null;
  hydrated: boolean;
  isLoading: boolean;
}

// Resolves the current selection (block_id + week label) into a concrete week
// object with start/end dates. Components that need to call /api/runs or
// /api/training/cycle-stats use this to get the week boundaries without
// re-implementing the lookup.
export function useCurrentWeek(): ResolvedWeek {
  const { blockId, weekLabel, hydrated } = useTrainingSelection();
  const weeksQuery = useQuery({
    queryKey: ["training", "weeks", blockId],
    queryFn: () =>
      apiGet<WeeksResponse>(
        `/api/training/weeks?block_id=${encodeURIComponent(blockId!)}`,
      ),
    enabled: hydrated && !!blockId,
  });
  const week =
    weeksQuery.data?.weeks.find((w) => w.label === weekLabel) ?? null;
  return {
    blockId,
    week,
    hydrated,
    isLoading: hydrated && (!blockId || weeksQuery.isLoading),
  };
}
