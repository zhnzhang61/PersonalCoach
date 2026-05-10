"use client";

import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Sparkles } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet, apiPost } from "@/lib/api";
import { useCoachSession } from "@/lib/hooks/use-coach-session";
import {
  extractFirstSentence,
  setTodaysRead,
  useTodaysRead,
} from "@/lib/todays-read";
import type {
  CoachActionResponse,
  HealthSnapshot,
  MetricSnapshot,
} from "@/lib/types";
import { cn } from "@/lib/utils";

// Today's read.
//
// Two modes for the headline text:
//   • Default: rule-based summary derived from snapshot metric tones
//     (cheap, always available, no LLM).
//   • Promoted: once the user taps the card today, we fire `review_health`
//     in the background and replace the headline with the first sentence
//     of the AI answer. Tap again that same day → just navigate to /coach
//     (the conversation already has the answer). Local midnight wipes
//     the cache and we're back to the rule-based default tomorrow.

interface Interpretation {
  headline: string;
  bullets: { text: string; tone: "good" | "bad" | "flat" }[];
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

  return { headline, bullets };
}

const TONE_BG: Record<string, string> = {
  good: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  bad: "bg-rose-500/15 text-rose-700 dark:text-rose-300",
  flat: "bg-muted text-muted-foreground",
};

export function ReadinessCard() {
  const router = useRouter();
  const { ensureCurrent } = useCoachSession();
  const cached = useTodaysRead();
  const aiHeadline =
    cached?.status === "ready" && cached.text ? cached.text : null;
  const pending = cached?.status === "pending";

  const { data, isLoading, error } = useQuery({
    queryKey: ["health", "snapshot", 14],
    queryFn: () => apiGet<HealthSnapshot>("/api/health/snapshot?baseline_days=14"),
  });

  const interpretation = data ? summarizeRules(data) : null;
  const headline = aiHeadline ?? interpretation?.headline ?? null;

  const onTap = () => {
    // If today's review_health is already done or in flight, just
    // navigate — don't double-fire. The conversation in /coach has
    // (or will have) the full reply.
    if (cached) {
      router.push("/coach");
      return;
    }

    const tid = ensureCurrent();
    setTodaysRead({ status: "pending", thread_id: tid });

    // Fire-and-forget. The promise resolves AFTER router.push has
    // taken the user to /coach; the .then writes the first sentence
    // back to localStorage so the next /health visit reflects it.
    apiPost<CoachActionResponse>("/api/ai/action/review_health", {
      thread_id: tid,
    })
      .then((res) => {
        if (res.error) {
          // Keep the pending marker so the spinner stays; the user
          // can manually re-trigger from the Coach pill if needed.
          return;
        }
        const sentence = extractFirstSentence(res.answer ?? "");
        if (sentence) {
          setTodaysRead({
            status: "ready",
            text: sentence,
            thread_id: tid,
          });
        }
      })
      .catch(() => {
        // Network blew up. Leave the pending marker — same rationale.
      });

    router.push("/coach");
  };

  return (
    <Card
      role="button"
      tabIndex={0}
      onClick={onTap}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onTap();
        }
      }}
      className="cursor-pointer bg-warm-bg/35 border-warm-accent/25 transition-colors hover:bg-warm-bg/50 active:bg-warm-bg/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-warm-accent/40"
    >
      <CardContent className="space-y-4 p-5 sm:p-6">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Sparkles className="size-4 text-warm-accent" aria-hidden />
            <span className="eyebrow">Today&rsquo;s read</span>
          </div>
          {pending && !aiHeadline ? (
            <Loader2 className="size-3.5 animate-spin text-muted-foreground" aria-hidden />
          ) : null}
        </div>

        {isLoading ? (
          <Skeleton className="h-8 w-3/4" />
        ) : error ? (
          <p className="text-sm text-rose-700 dark:text-rose-400">
            Couldn&rsquo;t load today&rsquo;s read: {(error as Error).message}
          </p>
        ) : !headline ? (
          <p className="text-sm text-muted-foreground">
            No snapshot data yet — sync to see today&rsquo;s read.
          </p>
        ) : (
          <h2 className="font-heading text-xl font-semibold leading-snug tracking-tight sm:text-2xl">
            {headline}
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
      </CardContent>
    </Card>
  );
}
