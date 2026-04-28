"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight } from "lucide-react";
import Link from "next/link";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import type {
  HealthTimelineResponse,
  SleepDetail as SleepDetailData,
} from "@/lib/types";
import { cn } from "@/lib/utils";

const STAGES = [
  { key: "deep_min" as const, label: "Deep", color: "bg-indigo-600", inverted: false },
  { key: "rem_min" as const, label: "REM", color: "bg-violet-500", inverted: false },
  { key: "light_min" as const, label: "Light", color: "bg-sky-400", inverted: false },
  { key: "awake_min" as const, label: "Awake", color: "bg-rose-300", inverted: true },
];

function fmtHM(mins: number | null | undefined): string {
  if (mins == null) return "—";
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  if (h === 0) return `${m}m`;
  return `${h}h ${m.toString().padStart(2, "0")}m`;
}

function delta(current: number, prior: number | null): {
  pct: number;
  label: string;
  tone: "up" | "down" | "flat";
} | null {
  if (prior == null || prior === 0) return null;
  const diff = current - prior;
  const pct = (diff / prior) * 100;
  const tone = Math.abs(pct) < 3 ? "flat" : pct > 0 ? "up" : "down";
  const sign = pct > 0 ? "+" : "";
  return { pct, label: `${sign}${pct.toFixed(0)}% vs 7d avg`, tone };
}

export function SleepDetailView() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["health", "sleep"],
    queryFn: () => apiGet<SleepDetailData>("/api/health/sleep"),
  });
  const timeline = useQuery({
    queryKey: ["health", "timeline", 7],
    queryFn: () =>
      apiGet<HealthTimelineResponse>("/api/health/timeline?days=7"),
  });

  return (
    <div className="mx-auto w-full max-w-3xl">
      <header className="px-3 pt-3 pb-3 sm:px-6 sm:pt-6">
        <Link
          href="/"
          className="-ml-1 inline-flex min-h-12 items-center gap-2 rounded-lg px-3 py-3 text-lg font-medium text-foreground/80 transition-colors hover:text-foreground active:bg-muted"
        >
          <ArrowLeft className="size-6" strokeWidth={2.25} aria-hidden />
          Health
        </Link>
        <div className="eyebrow mt-3 px-2">
          {data?.date ? `Night of ${fmtDate(data.date, "EEE, MMM d")}` : "Last night"}
        </div>
        <h1 className="font-heading mt-1 px-2 text-4xl font-semibold leading-[1.05] tracking-tight sm:text-5xl">
          Sleep
        </h1>
      </header>

      <div className="space-y-4 px-5 pb-6 sm:px-8">
        {error ? (
          <Card>
            <CardContent className="p-5 text-sm text-rose-600 dark:text-rose-400">
              Failed to load sleep details: {(error as Error).message}
            </CardContent>
          </Card>
        ) : (
          <>
            <Card className="bg-warm-bg/40 border-warm-accent/30">
              <CardContent className="space-y-5 p-6 sm:p-7">
                <div>
                  <div className="eyebrow">Total sleep</div>
                  {isLoading ? (
                    <Skeleton className="mt-1 h-12 w-40" />
                  ) : (
                    <div className="font-heading mt-1 whitespace-nowrap text-5xl font-semibold tabular-nums leading-none sm:text-6xl">
                      {fmtHM(data?.total_min)}
                    </div>
                  )}
                  {!isLoading && data && (
                    <div className="mt-2 space-y-0.5 text-sm text-muted-foreground">
                      {data.avg_7d.total_min != null && (
                        <div>
                          7-day average · {fmtHM(data.avg_7d.total_min)}
                        </div>
                      )}
                      {data.sleep_start && data.sleep_end && (
                        <div className="inline-flex items-center gap-1.5 tabular-nums">
                          <span className="font-medium text-foreground/80">
                            {data.sleep_start}
                          </span>
                          <ArrowRight
                            className="size-3.5 text-muted-foreground/70"
                            aria-hidden
                          />
                          <span className="font-medium text-foreground/80">
                            {data.sleep_end}
                          </span>
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {isLoading ? (
                  <Skeleton className="h-5 w-full rounded-full" />
                ) : (
                  data && <StageBar data={data} />
                )}

                <div className="grid grid-cols-4 gap-2">
                  {STAGES.map((s) => {
                    const v = data?.[s.key] ?? null;
                    const prior = data?.avg_7d[s.key] ?? null;
                    const d = v != null ? delta(v, prior) : null;
                    const goodTone = s.inverted ? "down" : "up";
                    return (
                      <div key={s.key} className="space-y-1">
                        <div className="flex items-center gap-1.5">
                          <span
                            className={cn("size-2 rounded-full", s.color)}
                            aria-hidden
                          />
                          <span className="eyebrow">{s.label}</span>
                        </div>
                        <div className="font-heading whitespace-nowrap text-base font-semibold tabular-nums leading-tight sm:text-2xl">
                          {isLoading ? (
                            <Skeleton className="h-6 w-12" />
                          ) : (
                            fmtHM(v)
                          )}
                        </div>
                        {d && (
                          <div
                            className={cn(
                              "text-[10px] font-medium",
                              d.tone === "flat"
                                ? "text-muted-foreground"
                                : d.tone === goodTone
                                  ? "text-emerald-700 dark:text-emerald-400"
                                  : "text-rose-700 dark:text-rose-400",
                            )}
                          >
                            {d.label}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </CardContent>
            </Card>

            <SleepHistoryBars
              timeline={timeline.data?.timeline}
              loading={timeline.isLoading}
              error={timeline.error as Error | null}
            />

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <DetailCard
                label="Body battery"
                value={
                  data?.body_battery_change != null
                    ? `${data.body_battery_change > 0 ? "+" : ""}${data.body_battery_change}`
                    : "—"
                }
                compare={
                  data?.body_battery_change != null
                    ? {
                        current: data.body_battery_change,
                        prior: data.avg_7d.body_battery_change,
                      }
                    : undefined
                }
                loading={isLoading}
              />
              <DetailCard
                label="Avg sleep HR"
                value={data?.avg_hr != null ? data.avg_hr.toFixed(0) : "—"}
                unit="bpm"
                compare={
                  data?.avg_hr != null
                    ? {
                        current: data.avg_hr,
                        prior: data.avg_7d.avg_hr,
                        inverted: true,
                      }
                    : undefined
                }
                loading={isLoading}
              />
              <DetailCard
                label="Awakenings"
                value={
                  data?.awake_count != null ? `${data.awake_count}` : "—"
                }
                compare={
                  data?.awake_count != null
                    ? {
                        current: data.awake_count,
                        prior: data.avg_7d.awake_count,
                        inverted: true,
                      }
                    : undefined
                }
                loading={isLoading}
              />
              <DetailCard
                label="Avg respiration"
                value={
                  data?.avg_respiration != null
                    ? data.avg_respiration.toFixed(1)
                    : "—"
                }
                unit="brpm"
                compare={
                  data?.avg_respiration != null
                    ? {
                        current: data.avg_respiration,
                        prior: data.avg_7d.avg_respiration,
                        inverted: true,
                      }
                    : undefined
                }
                loading={isLoading}
              />
              <DetailCard
                label="Sleep stress"
                value={
                  data?.sleep_stress != null
                    ? data.sleep_stress.toFixed(0)
                    : "—"
                }
                compare={
                  data?.sleep_stress != null
                    ? {
                        current: data.sleep_stress,
                        prior: data.avg_7d.sleep_stress,
                        inverted: true,
                      }
                    : undefined
                }
                loading={isLoading}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function StageBar({ data }: { data: SleepDetailData }) {
  const total =
    data.deep_min + data.rem_min + data.light_min + data.awake_min || 1;
  return (
    <div
      className="flex h-3 w-full overflow-hidden rounded-full bg-muted"
      role="img"
      aria-label="Sleep stage distribution"
    >
      {STAGES.map((s) => {
        const v = data[s.key];
        const pct = (v / total) * 100;
        if (pct < 0.5) return null;
        return (
          <div
            key={s.key}
            className={s.color}
            style={{ width: `${pct}%` }}
            title={`${s.label}: ${fmtHM(v)} (${pct.toFixed(0)}%)`}
          />
        );
      })}
    </div>
  );
}

function SleepHistoryBars({
  timeline,
  loading,
  error,
}: {
  timeline: HealthTimelineResponse["timeline"] | undefined;
  loading: boolean;
  error: Error | null;
}) {
  const nights = (timeline ?? []).filter((d) => d.sleep_hours != null);
  const max = nights.reduce(
    (m, d) => Math.max(m, d.sleep_hours ?? 0),
    8,
  );
  const avg =
    nights.length > 0
      ? nights.reduce((s, d) => s + (d.sleep_hours ?? 0), 0) / nights.length
      : null;

  return (
    <Card>
      <CardContent className="space-y-4 p-5 sm:p-6">
        <div className="flex items-baseline justify-between">
          <div>
            <div className="eyebrow">Last 7 nights</div>
            <div className="font-heading mt-1 text-xl font-semibold tracking-tight sm:text-2xl">
              Total sleep
            </div>
          </div>
          {avg != null && (
            <div className="text-xs text-muted-foreground tabular-nums">
              avg {avg.toFixed(1)}h
            </div>
          )}
        </div>

        {loading ? (
          <Skeleton className="h-32 w-full" />
        ) : error ? (
          <div className="text-sm text-rose-600 dark:text-rose-400">
            Failed to load history.
          </div>
        ) : nights.length === 0 ? (
          <div className="text-sm text-muted-foreground">
            No recent sleep data.
          </div>
        ) : (
          <div
            className="grid items-end gap-1.5"
            style={{ gridTemplateColumns: `repeat(${nights.length}, 1fr)` }}
            role="img"
            aria-label="Sleep hours per night, last 7 nights"
          >
            {nights.map((d) => {
              const hrs = d.sleep_hours ?? 0;
              const heightPct = max > 0 ? Math.max((hrs / max) * 100, 6) : 6;
              return (
                <div
                  key={d.date}
                  className="flex flex-col items-center gap-1.5"
                >
                  <div className="flex h-24 w-full items-end">
                    <div
                      className="w-full rounded-t-md bg-warm-accent/70"
                      style={{ height: `${heightPct}%` }}
                      title={`${fmtDate(d.date, "EEE, MMM d")}: ${hrs.toFixed(1)}h`}
                    />
                  </div>
                  <span className="text-[10px] font-medium tabular-nums text-foreground/80">
                    {hrs.toFixed(1)}
                  </span>
                  <span className="eyebrow text-[9px]">
                    {fmtDate(d.date, "EEE")}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function DetailCard({
  label,
  value,
  unit,
  hint,
  compare,
  loading,
}: {
  label: string;
  value: string;
  unit?: string;
  hint?: string;
  compare?: { current: number; prior: number | null; inverted?: boolean };
  loading?: boolean;
}) {
  const d = compare && compare.prior != null ? delta(compare.current, compare.prior) : null;
  const goodTone = compare?.inverted ? "down" : "up";
  const baseline =
    compare && compare.prior != null
      ? `7d ${formatPrior(compare.prior)}`
      : null;

  return (
    <Card>
      <CardContent className="space-y-1.5 p-5">
        <div className="eyebrow">{label}</div>
        {loading ? (
          <Skeleton className="h-9 w-20" />
        ) : (
          <div className="flex items-baseline gap-1.5">
            <span className="font-heading text-3xl font-semibold tabular-nums leading-none">
              {value}
            </span>
            {unit && (
              <span className="text-xs text-muted-foreground">{unit}</span>
            )}
          </div>
        )}
        {!loading && (d || baseline || hint) && (
          <div className="flex flex-wrap items-baseline gap-x-1.5 text-xs">
            {d && (
              <span
                className={cn(
                  "font-medium",
                  d.tone === "flat"
                    ? "text-muted-foreground"
                    : d.tone === goodTone
                      ? "text-emerald-700 dark:text-emerald-400"
                      : "text-rose-700 dark:text-rose-400",
                )}
              >
                {d.label.replace(" vs 7d avg", "")}
              </span>
            )}
            {(baseline || hint) && (
              <span className="text-muted-foreground">
                {baseline ?? hint}
              </span>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function formatPrior(n: number): string {
  // Drop the trailing .0 for whole-number 7d averages but keep one decimal
  // for fractional ones (e.g. "1.3" awakenings, "14.9" brpm).
  return n % 1 === 0 ? `${n}` : n.toFixed(1);
}
