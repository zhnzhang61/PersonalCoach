"use client";

import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import type { HealthTodayResponse } from "@/lib/types";
import { MetricCard } from "@/components/metric-card";

function hrvTone(status: string): "neutral" | "good" | "warn" | "bad" {
  const s = status.toLowerCase();
  if (s.includes("balanced") || s.includes("good")) return "good";
  if (s.includes("low") || s.includes("poor")) return "bad";
  if (s.includes("unbalanced") || s.includes("high")) return "warn";
  return "neutral";
}

function stressBucket(v: number): { label: string; tone: "good" | "warn" | "bad" } {
  if (v <= 25) return { label: "rest", tone: "good" };
  if (v <= 50) return { label: "low", tone: "good" };
  if (v <= 75) return { label: "med", tone: "warn" };
  return { label: "high", tone: "bad" };
}

function sleepTone(score: number | null): "neutral" | "good" | "warn" | "bad" {
  if (score == null) return "neutral";
  if (score >= 80) return "good";
  if (score >= 60) return "warn";
  return "bad";
}

export function TodayCards() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["health", "today"],
    queryFn: () => apiGet<HealthTodayResponse>("/api/health/today"),
  });

  if (error) {
    return (
      <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-700 dark:text-rose-300">
        Failed to load today&rsquo;s metrics: {(error as Error).message}
      </div>
    );
  }

  const today = data?.today;

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      <MetricCard
        label="Sleep"
        value={fmtNum(today?.sleep_score)}
        hint={
          today?.sleep_hours != null
            ? `${fmtNum(today.sleep_hours, 1)} hours`
            : undefined
        }
        badge={
          today?.sleep_score != null
            ? { text: "score", tone: sleepTone(today.sleep_score) }
            : undefined
        }
        loading={isLoading}
      />
      <MetricCard
        label="HRV"
        value={fmtNum(today?.hrv)}
        unit="ms"
        badge={
          data?.hrv_status
            ? { text: data.hrv_status, tone: hrvTone(data.hrv_status) }
            : undefined
        }
        loading={isLoading}
      />
      <MetricCard
        label="Resting HR"
        value={fmtNum(today?.rhr)}
        unit="bpm"
        loading={isLoading}
      />
      <MetricCard
        label="Stress"
        value={fmtNum(today?.stress)}
        badge={
          today?.stress != null
            ? (() => {
                const b = stressBucket(today.stress);
                return { text: b.label, tone: b.tone };
              })()
            : undefined
        }
        loading={isLoading}
      />
      <MetricCard
        label="Run Today"
        value={fmtNum(today?.run_miles, 1)}
        unit="mi"
        hint={
          today?.run_mins != null
            ? `${fmtNum(today.run_mins, 0)} min`
            : "no run today"
        }
        loading={isLoading}
        className="col-span-2 sm:col-span-1"
      />
    </div>
  );
}
