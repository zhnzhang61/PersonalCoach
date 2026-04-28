"use client";

import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import type { HealthSnapshot, MetricSnapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

// Today's read. v1 derives the headline + bullets from the snapshot's tones —
// the same data shape an LLM will eventually consume. The card's `interpretation`
// shape is deliberately a (headline, bullets) pair so we can swap the rule-based
// summary with /api/ai/health-snapshot output without touching this component.

interface Interpretation {
  headline: string;
  bullets: { text: string; tone: "good" | "bad" | "flat" }[];
  source: "rules" | "ai";
}

function metricLabel(m: MetricSnapshot, recent: NonNullable<MetricSnapshot["baselines"]["recent"]>): string {
  const sign = recent.delta_pct != null && recent.delta_pct > 0 ? "+" : "";
  const pct = recent.delta_pct != null ? `${sign}${recent.delta_pct.toFixed(0)}%` : "";
  return `${m.label} ${pct}`.trim();
}

function summarizeRules(snap: HealthSnapshot): Interpretation {
  const tones = snap.metrics
    .map((m) => ({ m, b: m.baselines.recent }))
    .filter((x) => x.b);
  const goods = tones.filter((x) => x.b!.tone === "good");
  const bads = tones.filter((x) => x.b!.tone === "bad");
  const flats = tones.filter((x) => x.b!.tone === "flat");

  let headline: string;
  if (bads.length === 0 && goods.length >= 2) {
    headline = "All recovery signals look healthy.";
  } else if (bads.length === 0) {
    headline = "Within normal range.";
  } else if (bads.length === 1) {
    headline = `One signal is off — most others look fine.`;
  } else if (bads.length >= goods.length + flats.length) {
    headline = `Multiple signals under-recovered. Consider an easy day.`;
  } else {
    headline = `Mixed signals. Read the details below.`;
  }

  const bullets: Interpretation["bullets"] = [
    ...bads.map((x) => ({
      text: metricLabel(x.m, x.b!),
      tone: "bad" as const,
    })),
    ...goods.map((x) => ({
      text: metricLabel(x.m, x.b!),
      tone: "good" as const,
    })),
    ...flats.map((x) => ({
      text: metricLabel(x.m, x.b!),
      tone: "flat" as const,
    })),
  ];

  return { headline, bullets, source: "rules" };
}

const TONE_BG: Record<string, string> = {
  good: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  bad: "bg-rose-500/15 text-rose-700 dark:text-rose-300",
  flat: "bg-muted text-muted-foreground",
};

export function ReadinessCard() {
  const { data, isLoading } = useQuery({
    queryKey: ["health", "snapshot", 14],
    queryFn: () => apiGet<HealthSnapshot>("/api/health/snapshot?baseline_days=14"),
  });

  const interpretation = data ? summarizeRules(data) : null;

  return (
    <Card className="bg-warm-bg/35 border-warm-accent/25">
      <CardContent className="space-y-4 p-5 sm:p-6">
        <div className="flex items-center gap-2">
          <Sparkles className="size-4 text-warm-accent" aria-hidden />
          <span className="eyebrow">Today&rsquo;s read</span>
        </div>

        {isLoading || !interpretation ? (
          <Skeleton className="h-8 w-3/4" />
        ) : (
          <h2 className="font-heading text-xl font-semibold leading-snug tracking-tight sm:text-2xl">
            {interpretation.headline}
          </h2>
        )}

        {interpretation && interpretation.bullets.length > 0 && (
          <ul className="flex flex-wrap gap-2">
            {interpretation.bullets.map((b, i) => (
              <li
                key={i}
                className={cn(
                  "rounded-full px-2.5 py-1 text-xs font-medium tabular-nums",
                  TONE_BG[b.tone],
                )}
              >
                {b.text}
              </li>
            ))}
          </ul>
        )}

        <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {interpretation?.source === "ai" ? "AI summary" : "Rule-based read"}
        </p>
      </CardContent>
    </Card>
  );
}
