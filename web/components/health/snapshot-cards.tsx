"use client";

import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import type {
  BaselineSummary,
  HealthSnapshot,
  MetricSnapshot,
  Tone,
} from "@/lib/types";
import { MetricCard } from "@/components/metric-card";
import { HrvBand } from "@/components/health/hrv-band";

const TONE_TEXT: Record<Tone, string> = {
  good: "good",
  bad: "off",
  flat: "stable",
  neutral: "—",
};

function fmtValue(m: MetricSnapshot): string {
  if (m.value == null) return "—";
  // Sleep score is already a whole number; HRV/RHR/stress are integers in
  // practice. Run today (when surfaced) keeps a decimal. Keep this dumb until
  // someone needs more.
  return Number.isInteger(m.value) ? `${m.value}` : m.value.toFixed(1);
}

function fmtBaseline(b: BaselineSummary, unit: string | null): string {
  if (b.value == null) return "no baseline yet";
  const v = Number.isInteger(b.value) ? `${b.value}` : b.value.toFixed(1);
  return unit ? `${b.days}d ${v}${unit}` : `${b.days}d ${v}`;
}

function fmtDelta(b: BaselineSummary): string | null {
  if (b.delta_pct == null) return null;
  const sign = b.delta_pct > 0 ? "+" : "";
  return `${sign}${b.delta_pct.toFixed(0)}%`;
}

function snapshotMetricToCardProps(m: MetricSnapshot) {
  const recent = m.baselines.recent;
  const delta = recent ? fmtDelta(recent) : null;
  const baseline = recent ? fmtBaseline(recent, m.unit) : null;
  // Re-use MetricCard's existing badge slot for the colored delta. The
  // baseline goes in the hint line below the value.
  return {
    label: m.label,
    value: fmtValue(m),
    unit: m.unit ?? undefined,
    badge:
      recent && recent.tone !== "neutral" && delta
        ? { text: delta, tone: recent.tone === "flat" ? "neutral" : recent.tone }
        : undefined,
    hint: baseline ?? undefined,
  } as const;
}

export function SnapshotCards() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["health", "snapshot", 14],
    queryFn: () => apiGet<HealthSnapshot>("/api/health/snapshot?baseline_days=14"),
  });

  if (error) {
    return (
      <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-700 dark:text-rose-300">
        Failed to load snapshot: {(error as Error).message}
      </div>
    );
  }

  const metrics = data?.metrics ?? [];
  const sleepCard = metrics.find((m) => m.key === "sleep_score");
  const otherCards = metrics.filter((m) => m.key !== "sleep_score");

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      {sleepCard ? (
        <MetricCard
          {...snapshotMetricToCardProps(sleepCard)}
          loading={isLoading}
          href="/health/sleep"
          // Override hint to keep the "tap for stages" affordance, with the
          // baseline tucked in alongside.
          hint={
            sleepCard.baselines.recent?.value != null
              ? `${sleepCard.baselines.recent.days}d ${sleepCard.baselines.recent.value} · tap for stages`
              : "tap for stages"
          }
        />
      ) : (
        <MetricCard label="Sleep" value="—" loading={isLoading} href="/health/sleep" />
      )}

      {otherCards.map((m) => {
        const footer =
          m.context?.type === "hrv_band" ? (
            <HrvBand value={m.value} context={m.context} />
          ) : undefined;
        return (
          <MetricCard
            key={m.key}
            {...snapshotMetricToCardProps(m)}
            footer={footer}
            loading={isLoading}
          />
        );
      })}
    </div>
  );
}

export { TONE_TEXT };
