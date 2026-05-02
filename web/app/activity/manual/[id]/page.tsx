"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Dumbbell,
  Footprints,
  Pencil,
  Sparkles,
  Waves,
  X,
} from "lucide-react";
import { apiDelete, apiGet, apiPut } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { fmtDate } from "@/lib/format";
import type { ManualActivity, ManualActivityType } from "@/lib/types";
import {
  ManualActivityForm,
  type ManualActivityFormValues,
} from "@/components/activity/manual-activity-form";

const TYPE_META: Record<string, { label: string; Icon: typeof Footprints }> = {
  run: { label: "Run", Icon: Footprints },
  swim: { label: "Swim", Icon: Waves },
  gym: { label: "Gym", Icon: Dumbbell },
  other: { label: "Other", Icon: Sparkles },
};

const KNOWN_TYPES: ManualActivityType[] = ["run", "swim", "gym", "other"];

export default function ManualActivityDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const qc = useQueryClient();
  const router = useRouter();
  const [editing, setEditing] = useState(false);

  // Same key shape as the run-detail page so the existing
  // `invalidateQueries({ queryKey: ["manual-activities"] })` calls in the
  // mutations below auto-refetch this on save without explicit listing.
  const detailQuery = useQuery({
    queryKey: ["manual-activities", id, "detail"],
    queryFn: () =>
      apiGet<ManualActivity>(
        `/api/manual-activities/${encodeURIComponent(id)}`,
      ),
  });

  const updateMut = useMutation({
    mutationFn: (payload: ManualActivityFormValues) =>
      apiPut<{ ok: boolean; activity: ManualActivity }>(
        `/api/manual-activities/${encodeURIComponent(id)}`,
        payload as unknown as Record<string, unknown>,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["manual-activities"] });
      setEditing(false);
    },
  });

  const deleteMut = useMutation({
    mutationFn: () =>
      apiDelete<{ ok: boolean }>(
        `/api/manual-activities/${encodeURIComponent(id)}`,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["manual-activities"] });
      router.push("/activity");
    },
  });

  if (detailQuery.isLoading) {
    return (
      <div className="mx-auto w-full max-w-4xl px-5 pt-8 pb-12 sm:px-8">
        <Skeleton className="mb-6 h-6 w-32" />
        <Skeleton className="mb-3 h-10 w-2/3" />
        <Skeleton className="mb-2 h-4 w-1/2" />
      </div>
    );
  }

  const activity = detailQuery.data;
  if (detailQuery.isError || !activity) {
    return (
      <div className="mx-auto w-full max-w-4xl px-5 pt-8 pb-12 sm:px-8">
        <Link
          href="/activity"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" />
          Back to activities
        </Link>
        <p className="mt-6 rounded-md border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-700 dark:text-rose-300">
          Could not load this activity.
        </p>
      </div>
    );
  }

  const meta = TYPE_META[activity.type] ?? {
    label: activity.type,
    Icon: Sparkles,
  };
  const Icon = meta.Icon;
  const safeType: ManualActivityType = (KNOWN_TYPES as string[]).includes(
    activity.type,
  )
    ? (activity.type as ManualActivityType)
    : "other";

  return (
    <div className="mx-auto w-full max-w-4xl px-5 pt-8 pb-12 sm:px-8">
      <div className="mb-4 flex items-center justify-between">
        <Link
          href="/activity"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" />
          Back to activities
        </Link>
        <button
          type="button"
          onClick={() => setEditing((v) => !v)}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-muted/40"
          aria-expanded={editing}
        >
          {editing ? (
            <>
              <X className="size-4" />
              Close
            </>
          ) : (
            <>
              <Pencil className="size-4" />
              Edit
            </>
          )}
        </button>
      </div>

      <div className="flex items-start gap-3">
        <div className="flex size-12 shrink-0 items-center justify-center rounded-md bg-muted/60">
          <Icon className="size-6 text-muted-foreground" aria-hidden />
        </div>
        <div className="min-w-0 flex-1 space-y-1">
          <h1 className="font-heading text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
            {meta.label}
          </h1>
          <p className="text-sm text-muted-foreground">
            {fmtDate(activity.date, "EEE MMM d")}
          </p>
          {(activity.distance_mi != null || activity.duration_min != null) && (
            <div className="flex flex-wrap gap-1.5 pt-1">
              {activity.distance_mi != null && (
                <Badge variant="outline" className="text-xs font-normal">
                  {activity.distance_mi.toFixed(2)} mi
                </Badge>
              )}
              {activity.duration_min != null && (
                <Badge variant="outline" className="text-xs font-normal">
                  {activity.duration_min} min
                </Badge>
              )}
            </div>
          )}
        </div>
      </div>

      {activity.desc && !editing && (
        <p className="mt-5 whitespace-pre-wrap rounded-md border border-border bg-muted/20 p-3 text-sm">
          {activity.desc}
        </p>
      )}

      {editing && (
        <div className="mt-5 rounded-md border border-border bg-muted/20 p-4">
          <ManualActivityForm
            title="Edit activity"
            saveLabel="Save"
            initial={{
              date: activity.date,
              type: safeType,
              description: activity.desc ?? "",
              duration_min: activity.duration_min,
              distance_mi: activity.distance_mi,
            }}
            pending={updateMut.isPending}
            deletePending={deleteMut.isPending}
            error={
              (updateMut.isError && (updateMut.error as Error).message) ||
              (deleteMut.isError && (deleteMut.error as Error).message) ||
              undefined
            }
            onSubmit={(v) => updateMut.mutate(v)}
            onDelete={() => deleteMut.mutate()}
            onCancel={() => setEditing(false)}
          />
        </div>
      )}
    </div>
  );
}
