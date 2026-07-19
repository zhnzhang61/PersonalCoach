"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import type { RunVerdict, VerdictsResponse } from "@/lib/types";

// Post-run verdict rows (PR #114). Attention rows are always visible;
// ok rows fold behind one "其余 N 项正常" line (user call — four rows
// were too much screen). Rows with an anchor are tappable: the page
// opens the telemetry drawer and highlights that window on the chart —
// the verdict is the sentence, the chart is its receipt.

export function useRunVerdicts(activityId: number) {
  return useQuery({
    queryKey: ["runs", activityId, "verdicts"],
    queryFn: () =>
      apiGet<VerdictsResponse>(`/api/runs/${activityId}/verdicts`),
    staleTime: 5 * 60_000,
    retry: false,
    enabled: Number.isFinite(activityId),
  });
}

function VerdictRow({
  v,
  onAnchorClick,
}: {
  v: RunVerdict;
  onAnchorClick?: (v: RunVerdict) => void;
}) {
  const clickable = v.anchor != null && onAnchorClick != null;
  const chip =
    v.status === "attention" ? (
      <span className="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-xs text-amber-700 dark:text-amber-300">
        看一眼
      </span>
    ) : (
      <span className="shrink-0 rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs text-emerald-700 dark:text-emerald-300">
        正常
      </span>
    );
  const body = (
    <>
      <span className="shrink-0 text-sm font-medium">{v.title}</span>
      {chip}
      <span className="min-w-0 flex-1 truncate text-left text-xs text-muted-foreground">
        {v.summary}
      </span>
      {clickable && (
        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      )}
    </>
  );
  if (!clickable) {
    return <div className="flex items-center gap-2 px-3 py-2">{body}</div>;
  }
  return (
    <button
      type="button"
      onClick={() => onAnchorClick(v)}
      className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-muted/40"
    >
      {body}
    </button>
  );
}

export function RunVerdicts({
  activityId,
  onAnchorClick,
}: {
  activityId: number;
  onAnchorClick?: (v: RunVerdict) => void;
}) {
  const { data, isLoading, isError } = useRunVerdicts(activityId);
  const [showFolded, setShowFolded] = useState(false);

  if (isLoading) {
    return <Skeleton className="h-10 w-full" />;
  }
  // Per-card invariant: error state distinct from "nothing fired".
  if (isError) {
    return (
      <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
        判断句暂不可用（服务端错误）。
      </div>
    );
  }
  if (!data) return null;

  const attention = data.verdicts.filter((v) => v.status === "attention");
  const normal = data.verdicts.filter((v) => v.status === "ok");
  const foldedCount = normal.length + data.not_fired.length;

  if (data.verdicts.length === 0 && data.not_fired.length === 0) return null;

  return (
    <div className="divide-y divide-border rounded-md border border-border">
      {attention.map((v) => (
        <VerdictRow key={v.key} v={v} onAnchorClick={onAnchorClick} />
      ))}
      {foldedCount > 0 && (
        <button
          type="button"
          onClick={() => setShowFolded((s) => !s)}
          aria-expanded={showFolded}
          className="flex w-full items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:text-foreground"
        >
          <span>
            {normal.length > 0
              ? `其余 ${normal.length} 项正常`
              : "本次无点亮的判断"}
            {data.not_fired.length > 0 &&
              ` · ${data.not_fired.length} 项未触发`}
          </span>
          <span aria-hidden>{showFolded ? "▾" : "▸"}</span>
        </button>
      )}
      {showFolded && (
        <>
          {normal.map((v) => (
            <VerdictRow key={v.key} v={v} onAnchorClick={onAnchorClick} />
          ))}
          {data.not_fired.map((n) => (
            <div
              key={n.key}
              className="flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground"
            >
              <span className="shrink-0 font-medium">{n.title}</span>
              <span className="min-w-0 flex-1 truncate">{n.reason}</span>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
