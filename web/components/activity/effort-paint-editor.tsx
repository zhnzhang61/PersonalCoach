"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import { REST_COLOR, effortColor } from "@/lib/effort-colors";
import {
  EFFORT_CATEGORIES,
  type Lap,
  type RunActivity,
  type TreadmillEstimate,
} from "@/lib/types";
import { isTreadmillRun } from "@/components/activity/run-summary-block";

// Paint-style lap labeler (v3 design): the rows ARE the final visual —
// same bars as the summary block — and the brush palette sits BELOW the
// list, in thumb reach. Tap a row to paint it with the active brush;
// press-and-hold then drag to sweep a range. Nothing persists until the
// parent's Save.
export function EffortPaintEditor({
  run,
  laps,
  categories,
  onPaint,
  onSetAll,
}: {
  run: RunActivity;
  laps: Lap[];
  categories: string[];
  onPaint: (indices: number[], category: string) => void;
  onSetAll: (categories: string[]) => void;
}) {
  const [brush, setBrush] = useState<string>("Hold Back Easy");
  const [prefilling, setPrefilling] = useState(false);
  const [prefillError, setPrefillError] = useState(false);
  const treadmill = isTreadmillRun(run);

  const prefill = async () => {
    setPrefilling(true);
    setPrefillError(false);
    try {
      const res = await apiGet<{ categories: string[] }>(
        `/api/runs/${run.activityId}/suggest-labels`,
      );
      onSetAll(res.categories);
    } catch {
      setPrefillError(true);
    } finally {
      setPrefilling(false);
    }
  };

  // Treadmill rows show MODEL pace/HR (watch pace is fiction indoors);
  // display-only — categories still index Garmin laps either way.
  const estimateQuery = useQuery({
    queryKey: ["runs", run.activityId, "treadmill-estimate"],
    queryFn: () =>
      apiGet<TreadmillEstimate>(
        `/api/runs/${run.activityId}/treadmill-estimate`,
      ),
    staleTime: 5 * 60_000,
    retry: false,
    enabled: treadmill,
  });
  const modelLaps = estimateQuery.data?.estimate.laps;

  const rowData = laps.map((lap, i) => {
    const model = modelLaps?.[i];
    const mi = model?.model_distance_mi ?? lap.distance / 1609.34;
    const paceS =
      model?.pace_s ??
      (mi > 0.01 && lap.duration > 0 ? lap.duration / mi : null);
    return {
      idx: i,
      paceS,
      paceStr: paceS
        ? `${Math.floor(paceS / 60)}:${String(Math.round(paceS) % 60).padStart(2, "0")}`
        : "—",
      hr: model?.avg_hr ?? lap.averageHR ?? null,
      mi,
    };
  });

  const paced = rowData.filter(
    (r) => categories[r.idx] !== "Rest" && r.paceS != null,
  );
  const fast = Math.min(...paced.map((r) => r.paceS!));
  const slow = Math.max(...paced.map((r) => r.paceS!));
  const widthPct = (p: number | null) =>
    p == null || slow === fast ? 100 : 40 + (60 * (slow - p)) / (slow - fast);

  // --- paint gestures -----------------------------------------------
  // Tap = paint one row. Press-and-hold (350ms) then drag = sweep.
  // While sweeping we preventDefault touchmove so the page doesn't
  // scroll under the finger — via a non-passive listener, since React's
  // synthetic touch events are passive by default.
  const listRef = useRef<HTMLDivElement>(null);
  const sweeping = useRef(false);
  const holdTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastPainted = useRef<number | null>(null);
  const [sweepUi, setSweepUi] = useState(false);

  const endSweep = () => {
    if (holdTimer.current) clearTimeout(holdTimer.current);
    holdTimer.current = null;
    sweeping.current = false;
    lastPainted.current = null;
    setSweepUi(false);
  };

  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const onTouchMove = (e: TouchEvent) => {
      if (sweeping.current) e.preventDefault();
    };
    el.addEventListener("touchmove", onTouchMove, { passive: false });
    window.addEventListener("pointerup", endSweep);
    return () => {
      el.removeEventListener("touchmove", onTouchMove);
      window.removeEventListener("pointerup", endSweep);
    };
  }, []);

  const paint = (idx: number) => {
    if (lastPainted.current === idx) return;
    lastPainted.current = idx;
    onPaint([idx], brush);
  };

  const rowFromPoint = (x: number, y: number): number | null => {
    const hit = document
      .elementFromPoint(x, y)
      ?.closest("[data-lap-idx]") as HTMLElement | null;
    if (!hit) return null;
    return Number(hit.dataset.lapIdx);
  };

  const onPointerDown = (idx: number) => {
    holdTimer.current = setTimeout(() => {
      sweeping.current = true;
      setSweepUi(true);
      paint(idx);
    }, 350);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (!sweeping.current) return;
    const idx = rowFromPoint(e.clientX, e.clientY);
    if (idx != null) paint(idx);
  };

  const onRowClick = (idx: number) => {
    // A sweep that just ended fires a click on the release row — the
    // sweep already painted it, don't double-fire with a stale brush.
    if (sweeping.current || lastPainted.current === idx) {
      lastPainted.current = null;
      return;
    }
    onPaint([idx], brush);
  };

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-[2.5rem_3.25rem_1fr_2.5rem] items-center gap-x-2 text-xs uppercase tracking-wide text-muted-foreground">
        <span>Lap</span>
        <span>Pace</span>
        <span />
        <span className="text-right">HR</span>
      </div>
      <div
        ref={listRef}
        className="space-y-0"
        onPointerMove={onPointerMove}
        onPointerLeave={endSweep}
      >
        {rowData.map((r) => {
          const cat = categories[r.idx];
          const isRest = cat === "Rest";
          return (
            <div
              key={r.idx}
              data-lap-idx={r.idx}
              onPointerDown={() => onPointerDown(r.idx)}
              onClick={() => onRowClick(r.idx)}
              className={`grid cursor-pointer select-none grid-cols-[2.5rem_3.25rem_1fr_2.5rem] items-center gap-x-2 rounded py-2.5 ${
                sweepUi ? "bg-muted/30" : "hover:bg-muted/30"
              }`}
              title={`lap ${r.idx + 1} → ${brush}`}
            >
              <span
                className={`text-xs ${isRest ? "text-[10px]" : ""} text-muted-foreground`}
              >
                {isRest ? "rest" : r.idx + 1}
              </span>
              <span className="font-mono text-xs font-semibold">
                {isRest ? "" : r.paceStr}
              </span>
              {isRest ? (
                <div
                  className="h-1 w-[18%] rounded-full"
                  style={{ backgroundColor: REST_COLOR }}
                />
              ) : (
                <div
                  className="h-3.5 rounded-full"
                  style={{
                    width: `${widthPct(r.paceS)}%`,
                    backgroundColor: effortColor(cat),
                  }}
                />
              )}
              <span className="text-right font-mono text-xs text-muted-foreground">
                {r.hr ?? "—"}
              </span>
            </div>
          );
        })}
      </div>

      {/* Brush palette BELOW the list — thumb zone. */}
      <div className="border-t border-border pt-2">
        <div className="flex flex-wrap items-center gap-1.5">
          {EFFORT_CATEGORIES.map((c) => {
            const active = brush === c;
            const color = c === "Rest" ? REST_COLOR : effortColor(c);
            return (
              <button
                key={c}
                type="button"
                onClick={() => setBrush(c)}
                aria-label={`Brush: ${c}`}
                aria-pressed={active}
                title={c}
                className={`flex items-center gap-1.5 rounded-md px-3 py-2 text-xs transition-transform ${
                  active
                    ? "scale-105 ring-2 ring-foreground/60 ring-offset-1 ring-offset-background"
                    : "opacity-80 hover:opacity-100"
                }`}
                style={{ backgroundColor: `${color}33` }}
              >
                <span
                  className="size-4 shrink-0 rounded"
                  style={{ backgroundColor: color }}
                />
                {c}
              </button>
            );
          })}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => onSetAll(laps.map(() => brush))}
            className="rounded-md border border-border px-2 py-1 text-xs hover:bg-muted/40"
          >
            Paint all: {brush}
          </button>
          <button
            type="button"
            onClick={prefill}
            disabled={prefilling}
            className="rounded-md border border-border px-2 py-1 text-xs hover:bg-muted/40 disabled:opacity-50"
          >
            {prefilling ? "Prefilling…" : "Prefill from HR"}
          </button>
          {prefillError && (
            <span className="text-xs text-rose-700 dark:text-rose-300">
              Prefill failed — try again.
            </span>
          )}
        </div>
        <p className="mt-1.5 text-[11px] text-muted-foreground">
          Tap a lap to paint it with the active brush · press-hold and drag
          to sweep a range
        </p>
      </div>
    </div>
  );
}
