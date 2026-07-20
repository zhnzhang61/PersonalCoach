"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Lightbulb } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import type { CoachingTip, CoachingTipsResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

// Coaching tips card — distilled takeaways from coaching
// conversations. Read-only by design: rows are appended by the coach
// (agent chat / Claude Code session) via POST /api/coaching-tips
// after a discussion converges, so this card is the durable summary
// of "what we agreed" without scrolling back through chat history.
//
// Newest first (API sorts). Rows collapse to title + topic chip;
// tapping expands the body. The newest tip starts expanded — it's
// the one the user most likely just discussed.

export function CoachingTips() {
  const query = useQuery({
    queryKey: ["coaching-tips"],
    queryFn: () => apiGet<CoachingTipsResponse>("/api/coaching-tips"),
    staleTime: 30_000,
  });

  // null = "no explicit choice yet" → newest tip auto-expands once
  // data lands. A Set would allow multi-expand; single-slot keeps the
  // card compact on the phone where it's mostly read.
  const [expanded, setExpanded] = useState<string | null>(null);

  const tips = query.data?.tips ?? [];
  const effectiveExpanded =
    expanded ?? (tips.length > 0 ? tips[0].id : null);

  return (
    <Card>
      <CardHeader className="space-y-0">
        <div className="flex items-center gap-2">
          <Lightbulb className="size-4 text-warm-accent" />
          <CardTitle className="text-base">Coaching tips</CardTitle>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          Takeaways distilled from coaching conversations.
        </p>
      </CardHeader>
      <CardContent>
        {query.isLoading && !query.data ? (
          <Skeleton className="h-20 w-full" />
        ) : query.isError ? (
          // Same fetch-failure-vs-empty split every card carries
          // (Codex P3 lesson from #80) — a network blip must not
          // masquerade as "no tips yet".
          <p className="py-4 text-center text-sm text-rose-600 dark:text-rose-400">
            Couldn&rsquo;t load tips —{" "}
            {(query.error as Error | null)?.message ?? "please retry."}
          </p>
        ) : tips.length === 0 ? (
          <p className="py-4 text-center text-sm text-muted-foreground">
            Nothing here yet — tips land as coaching discussions
            conclude.
          </p>
        ) : (
          <ul className="space-y-2">
            {tips.map((tip) => (
              <li key={tip.id}>
                <TipRow
                  tip={tip}
                  expanded={effectiveExpanded === tip.id}
                  onToggle={() =>
                    setExpanded(
                      effectiveExpanded === tip.id ? "" : tip.id,
                    )
                  }
                />
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

interface TipRowProps {
  tip: CoachingTip;
  expanded: boolean;
  onToggle: () => void;
}

function TipRow({ tip, expanded, onToggle }: TipRowProps) {
  return (
    <div className="rounded-md border border-border bg-background">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 p-3 text-left transition-colors hover:bg-muted/30"
      >
        <div className="min-w-0 flex-1 space-y-0.5">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="text-sm font-medium">{tip.title}</span>
            {tip.topic && (
              <span className="rounded-full bg-muted/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {tip.topic}
              </span>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground">{tip.date}</p>
        </div>
        <ChevronDown
          className={cn(
            "size-4 shrink-0 text-muted-foreground transition-transform",
            expanded && "rotate-180",
          )}
          aria-hidden
        />
      </button>
      {expanded && (
        <p className="whitespace-pre-line border-t border-border px-3 py-2.5 text-xs leading-relaxed text-muted-foreground">
          {tip.body}
        </p>
      )}
    </div>
  );
}
