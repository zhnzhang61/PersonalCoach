"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import type { SleepDetail as SleepDetailData } from "@/lib/types";
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
                  {data?.avg_7d.total_min != null && !isLoading && (
                    <div className="mt-2 text-sm text-muted-foreground">
                      7-day average · {fmtHM(data.avg_7d.total_min)}
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

            <div className="grid grid-cols-2 gap-3">
              <DetailCard
                label="Avg respiration"
                value={
                  data?.avg_respiration != null
                    ? data.avg_respiration.toFixed(1)
                    : "—"
                }
                unit="brpm"
                hint={
                  data?.avg_7d.avg_respiration != null
                    ? `7d ${data.avg_7d.avg_respiration.toFixed(1)}`
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
                hint={
                  data?.avg_7d.sleep_stress != null
                    ? `7d ${data.avg_7d.sleep_stress.toFixed(0)}`
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

function DetailCard({
  label,
  value,
  unit,
  hint,
  loading,
}: {
  label: string;
  value: string;
  unit?: string;
  hint?: string;
  loading?: boolean;
}) {
  return (
    <Card>
      <CardContent className="space-y-2 p-5">
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
        {hint && !loading && (
          <div className="text-xs text-muted-foreground">{hint}</div>
        )}
      </CardContent>
    </Card>
  );
}
