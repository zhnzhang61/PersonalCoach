"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, Sparkles } from "lucide-react";
import { apiPost } from "@/lib/api";
import { classifyCoachError } from "@/lib/coach-errors";
import { useCoachSession } from "@/lib/hooks/use-coach-session";
import type { CoachActionResponse } from "@/lib/types";

interface Props {
  activityId: number;
  // Optional: pass run start-date so the agent's review prompt has a
  // concrete date even before MCP tools are called.
  runDate?: string | null;
  // Optional: human-readable run name (e.g. "Weehawken Running"). Used
  // to phrase the chat-bubble that lands in /coach as a sentence the
  // user would have written ("请分析我 2026年5月10日 Weehawken
  // Running 这次训练。") instead of a raw activity_id reference.
  runName?: string | null;
}

/**
 * Format an ISO date (YYYY-MM-DD or full timestamp) as a Chinese
 * date phrase: "2026年5月10日". Returns null if the input doesn't
 * start with a parseable date.
 */
function formatRunDateZh(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return null;
  const [, y, mo, d] = m;
  // Drop leading zeros from month/day for a more natural Chinese reading.
  return `${y}年${parseInt(mo, 10)}月${parseInt(d, 10)}日`;
}

/**
 * "Ask AI about this run" button. Lives on the activity detail page
 * (where the activity_id is known); kicks off a review_workout action
 * inside the user's *current* coach session, then navigates to the
 * Coach tab so they can read the response and follow up.
 *
 * Per design doc: review_workout is launched here, not from the Coach
 * tab pills — the coach tab doesn't know which run the user means.
 */
export function AskAiButton({ activityId, runDate, runName }: Props) {
  const router = useRouter();
  const { ensureCurrent } = useCoachSession();
  const [pending, setPending] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const onClick = async () => {
    if (pending) return;
    setErrorMsg(null);
    setPending(true);
    const tid = ensureCurrent();

    // Build the user-facing message that will render as a chat bubble
    // in /coach. Falls back to the raw activity_id form only if we
    // genuinely have nothing readable.
    const dateZh = formatRunDateZh(runDate);
    const descriptor = [dateZh, runName?.trim()].filter(Boolean).join(" ");
    const message = descriptor
      ? `请分析我 ${descriptor} 这次训练。`
      : `请分析我 activity_id=${activityId} 这次训练。`;

    // Single rate-limit-aware retry. Same pattern as the coach thread:
    // if the agent's first call hits Gemini's 15 RPM, wait the
    // suggested cooldown and try once more before showing the error.
    const fire = () =>
      apiPost<CoachActionResponse>("/api/ai/action/review_workout", {
        thread_id: tid,
        activity_id: activityId,
        run_date: runDate ?? undefined,
        message,
      });

    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const res = await fire();
        if (res.error) {
          const info = classifyCoachError(res.error);
          if (info.kind === "rate_limit" && attempt === 0 && info.retryAfterSec) {
            setErrorMsg(info.message);
            await new Promise((r) => setTimeout(r, info.retryAfterSec! * 1000));
            continue;
          }
          setErrorMsg(info.message);
          setPending(false);
          return;
        }
        // Action succeeded — jump to Coach tab. The thread query there
        // will pick up the appended turn for this thread_id.
        router.push("/coach");
        return;
      } catch (e) {
        const info = classifyCoachError((e as Error).message);
        if (info.kind === "rate_limit" && attempt === 0 && info.retryAfterSec) {
          setErrorMsg(info.message);
          await new Promise((r) => setTimeout(r, info.retryAfterSec! * 1000));
          continue;
        }
        setErrorMsg(info.message);
        setPending(false);
        return;
      }
    }
    setPending(false);
  };

  return (
    <div className="space-y-1.5">
      <button
        type="button"
        onClick={onClick}
        disabled={pending}
        className="inline-flex items-center gap-1.5 rounded-full border border-warm-accent/40 bg-warm-accent/10 px-3.5 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-warm-accent/20 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {pending ? (
          <Loader2 className="size-4 animate-spin" aria-hidden />
        ) : (
          <Sparkles className="size-4" aria-hidden />
        )}
        {pending ? "Asking coach…" : "Ask AI about this run"}
      </button>
      {errorMsg && (
        <p className="text-xs text-rose-700 dark:text-rose-300">{errorMsg}</p>
      )}
    </div>
  );
}
