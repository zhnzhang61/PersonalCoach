"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Loader2 } from "lucide-react";
import { apiDelete, apiGet, apiPost, streamSSE } from "@/lib/api";
import { classifyCoachError } from "@/lib/coach-errors";
import { useCoachSession } from "@/lib/hooks/use-coach-session";
import type {
  CoachActionName,
  CoachActionResponse,
  CoachHistoryResponse,
  CoachMessage,
  CoachSession,
  CoachSessionsResponse,
} from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { MessageBubble } from "./message-bubble";
import { SessionDivider } from "./session-divider";
import { DayDivider } from "./day-divider";
import { ActionPills } from "./action-pills";
import { ChatInput } from "./chat-input";

// How many closed sessions to render above the active session by
// default. Per design doc: "load the most recent 3 sessions on first
// render". A "Load earlier" button extends backward.
const INITIAL_CLOSED = 3;

interface ThreadView {
  session: CoachSession;
  messages: CoachMessage[];
}

export function CoachThread() {
  const qc = useQueryClient();
  const { currentId, ensureCurrent, clearCurrent, hydrated } = useCoachSession();

  // Sessions list (closed + maybe active). The current session is in
  // here too — we filter it out and treat it specially.
  const sessionsQuery = useQuery({
    queryKey: ["coach", "sessions", INITIAL_CLOSED],
    queryFn: () =>
      apiGet<CoachSessionsResponse>(
        `/api/ai/sessions?limit=${INITIAL_CLOSED + 2}`,
      ),
    enabled: hydrated,
    staleTime: 0,
  });
  // Closed sessions (not the active one) — rendered above the active.
  // Reading `data?.sessions` directly inside the memo (rather than
  // capturing it in a parent variable) keeps the dependency stable
  // across renders that didn't actually change the query result.
  const closedSessions = useMemo(
    () =>
      (sessionsQuery.data?.sessions ?? [])
        .filter((s) => s.thread_id !== currentId && s.closed_at)
        .slice(0, INITIAL_CLOSED),
    [sessionsQuery.data, currentId],
  );

  // Active session messages: the live thread we'll append to.
  const activeQuery = useQuery({
    queryKey: ["coach", "history", currentId],
    queryFn: () =>
      apiGet<CoachHistoryResponse>(
        `/api/ai/history/${encodeURIComponent(currentId!)}`,
      ),
    enabled: !!currentId,
    staleTime: 0,
  });
  const activeMessages = activeQuery.data?.messages ?? [];

  // Closed-session message bodies, fetched lazily — only after we
  // know which closed sessions to render.
  const closedHistories = useQuery({
    queryKey: [
      "coach",
      "histories",
      closedSessions.map((s) => s.thread_id).join(","),
    ],
    queryFn: async (): Promise<ThreadView[]> => {
      const out: ThreadView[] = [];
      for (const s of closedSessions) {
        try {
          const h = await apiGet<CoachHistoryResponse>(
            `/api/ai/history/${encodeURIComponent(s.thread_id)}`,
          );
          out.push({ session: s, messages: h.messages });
        } catch {
          out.push({ session: s, messages: [] });
        }
      }
      return out;
    },
    enabled: closedSessions.length > 0,
    staleTime: 60_000,
  });

  // -- Action / chat invocation -----------------------------------

  const [pending, setPending] = useState<null | "chat" | CoachActionName>(null);
  const [archiveToast, setArchiveToast] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Optimistic state during a streaming chat turn. While the SSE
  // stream is open we show the user's just-sent message + an
  // accumulating AI bubble so the user sees tokens arriving instead
  // of staring at a spinner for 10s. On `done` we drop this and
  // refresh /api/ai/history to pick up the canonical messages (with
  // ts from PR #71).
  const [streamingTurn, setStreamingTurn] = useState<{
    userMessage: string;
    aiContent: string;
  } | null>(null);

  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: ["coach"] });
  };

  /**
   * Delete an archived session from the user's history.
   *
   * Confirms with a native dialog (this is rare admin-ish action;
   * native confirm is fine on mobile). Removes verbatim history +
   * session_meta on the backend; long-term lessons (CME topics /
   * episodes) deliberately remain — they're commingled with other
   * sessions. After delete, invalidate queries so the divider goes
   * away.
   */
  const deleteArchivedSession = async (threadId: string) => {
    if (!window.confirm("删除这次归档会话的对话记录？\n（学到的长期记忆不会被删除。）")) {
      return;
    }
    try {
      await apiDelete(`/api/ai/sessions/${encodeURIComponent(threadId)}`);
      refreshAll();
    } catch (e) {
      setErrorMsg((e as Error).message);
    }
  };

  /**
   * Run a coach call with rate-limit-aware single retry.
   *
   * The agent sometimes hits Gemini's 15 RPM ceiling — when it does we
   * want to (a) tell the user nicely, (b) wait the suggested cooldown,
   * (c) try once more, and only then surface the failure. Both the
   * thrown-error case (proxy 5xx) and the action endpoint's "200 with
   * `.error` body" case are handled uniformly.
   *
   * Returns the resolved value on success, or null on terminal failure
   * (caller has already had errorMsg set for them).
   */
  const callWithRetry = async <T,>(
    fn: () => Promise<T>,
    getErrorFromResult: (result: T) => string | null | undefined,
  ): Promise<T | null> => {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const value = await fn();
        const embedded = getErrorFromResult(value);
        if (embedded) {
          const info = classifyCoachError(embedded);
          if (info.kind === "rate_limit" && attempt === 0 && info.retryAfterSec) {
            setErrorMsg(info.message);
            await new Promise((r) => setTimeout(r, info.retryAfterSec! * 1000));
            continue;
          }
          setErrorMsg(info.message);
          return null;
        }
        setErrorMsg(null);
        return value;
      } catch (e) {
        const info = classifyCoachError((e as Error).message);
        if (info.kind === "rate_limit" && attempt === 0 && info.retryAfterSec) {
          setErrorMsg(info.message);
          await new Promise((r) => setTimeout(r, info.retryAfterSec! * 1000));
          continue;
        }
        setErrorMsg(info.message);
        return null;
      }
    }
    return null;
  };

  /**
   * Stream a chat turn over SSE. Returns:
   *   "ok"               — stream completed cleanly
   *   ErrInfo            — failed; caller decides whether to retry
   * Side effects: appends tokens into `streamingTurn.aiContent` so
   * the live bubble renders progress.
   */
  const tryStreamChat = async (
    tid: string,
    text: string,
  ): Promise<"ok" | ReturnType<typeof classifyCoachError>> => {
    setStreamingTurn({ userMessage: text, aiContent: "" });
    let streamError: ReturnType<typeof classifyCoachError> | null = null;
    try {
      await streamSSE(
        "/api/ai/chat/stream",
        { thread_id: tid, message: text },
        (ev) => {
          if (ev.type === "token") {
            setStreamingTurn((prev) =>
              prev ? { ...prev, aiContent: prev.aiContent + ev.content } : prev,
            );
          } else if (ev.type === "error") {
            streamError = classifyCoachError(ev.message);
          }
          // tool_call + done are informational here — `done` is
          // implicit when the SSE source closes; we let the loop
          // exit on its own.
        },
      );
    } catch (e) {
      return classifyCoachError((e as Error).message);
    }
    return streamError ?? "ok";
  };

  const sendChat = async (text: string) => {
    const tid = ensureCurrent();
    setPending("chat");
    setErrorMsg(null);

    // Single retry on rate-limit, mirroring the old sync flow's
    // callWithRetry behavior. We can't retry mid-stream (partial
    // tokens already rendered), so retry means a fresh attempt with
    // a fresh streaming bubble.
    for (let attempt = 0; attempt < 2; attempt++) {
      const result = await tryStreamChat(tid, text);
      if (result === "ok") {
        setErrorMsg(null);
        break;
      }
      if (
        result.kind === "rate_limit" &&
        attempt === 0 &&
        result.retryAfterSec
      ) {
        setErrorMsg(result.message);
        setStreamingTurn(null); // clear partial bubble before retry
        await new Promise((r) => setTimeout(r, result.retryAfterSec! * 1000));
        continue;
      }
      setErrorMsg(result.message);
      break;
    }

    setStreamingTurn(null);
    refreshAll();
    setPending(null);
  };

  const runAction = async (name: CoachActionName) => {
    if (name === "summarize_and_archive") {
      // Archive uses the existing currentId — there's nothing to
      // archive if we don't have one.
      if (!currentId) return;
      setPending(name);
      setErrorMsg(null);
      const res = await callWithRetry(
        () =>
          apiPost<CoachActionResponse>(`/api/ai/action/${name}`, {
            thread_id: currentId,
          }),
        (r) => r.error,
      );
      if (res && !res.error) {
        const lines: string[] = [];
        if (res.summary) lines.push(res.summary);
        const tags: string[] = [];
        if (res.topics_added) tags.push(`+${res.topics_added} topic${res.topics_added === 1 ? "" : "s"}`);
        if (res.episodes_added) tags.push(`+${res.episodes_added} episode${res.episodes_added === 1 ? "" : "s"}`);
        if (tags.length) lines.push(tags.join(" · "));
        setArchiveToast(lines.join("\n") || "Session archived.");
        // Clear active id so the next message creates a new session.
        clearCurrent();
      }
      refreshAll();
      setPending(null);
      return;
    }

    const tid = ensureCurrent();
    setPending(name);
    setErrorMsg(null);
    await callWithRetry(
      () =>
        apiPost<CoachActionResponse>(`/api/ai/action/${name}`, {
          thread_id: tid,
        }),
      (r) => r.error,
    );
    refreshAll();
    setPending(null);
  };

  // Auto-dismiss archive toast after 6s.
  useEffect(() => {
    if (!archiveToast) return;
    const t = setTimeout(() => setArchiveToast(null), 6000);
    return () => clearTimeout(t);
  }, [archiveToast]);

  // Scroll-to-bottom on message append OR on streaming progress.
  // Watching `streamingTurn?.aiContent.length` keeps the view pinned
  // to the latest tokens as they arrive — without this the user has
  // to manually scroll while the agent is writing.
  const bottomRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [activeMessages.length, pending, streamingTurn?.aiContent.length]);

  // Filter out tool/system messages from display — those are agent
  // internals, the user sees only human/ai turns.
  const visible = (msgs: CoachMessage[]): CoachMessage[] =>
    msgs.filter((m) => m.role === "human" || m.role === "ai");

  // Render a message stream with day-boundary dividers inserted at
  // calendar-day transitions (local timezone). Without this, a session
  // that spans multiple days reads as a flat wall with no temporal
  // anchor — exactly the "中间是昨天问的问题" symptom this PR fixes.
  // Messages without a ts (legacy data, pre-PR-A checkpoints) get no
  // anchor and don't trigger a divider, preserving prior behavior.
  const renderWithDayDividers = (msgs: CoachMessage[]) => {
    const out: ReactNode[] = [];
    let prevDay: string | null = null;
    msgs.forEach((m, i) => {
      const day = m.ts ? new Date(m.ts).toLocaleDateString() : null;
      if (day && day !== prevDay) {
        out.push(<DayDivider key={`day-${i}-${day}`} iso={m.ts!} />);
        prevDay = day;
      }
      out.push(<MessageBubble key={`msg-${i}`} message={m} />);
    });
    return out;
  };

  return (
    // Plain block (NOT flex flex-col). A `flex flex-col` parent silently
    // breaks `position: sticky` on its first child in mobile Safari (and
    // some Chrome cases) — that was the first cause of the "bar scrolls
    // away" report. The flex layout wasn't load-bearing here. Keep min-h
    // so short sessions still anchor the input row.
    <div className="min-h-[calc(100vh-180px)]">
      {/* Pinned top region: page title + subtitle + action pills + End &
        * Save. Rendered as ONE sticky wrapper so they all stay together
        * as the conversation scrolls underneath. (Page-level PageHeader
        * was removed from coach/page.tsx and pulled in here for exactly
        * this — without it, the title would sit ABOVE the sticky context
        * and scroll out separately.) */}
      <div className="sticky top-0 z-30 -mx-5 mb-3 border-b border-border bg-background/95 backdrop-blur-md sm:-mx-8">
        <PageHeader
          title="Coach"
          subtitle="Talk through training, health, and your week. The coach remembers what matters."
        />
        <div className="flex items-start justify-between gap-2 px-5 pb-2 sm:px-8">
          <ActionPills
            onAction={runAction}
            disabled={pending !== null}
          />
          <button
            type="button"
            onClick={() => runAction("summarize_and_archive")}
            disabled={pending !== null || !currentId || activeMessages.length < 2}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-xs font-medium text-rose-700 transition-colors hover:bg-rose-500/20 disabled:opacity-40 disabled:cursor-not-allowed dark:text-rose-300"
            title={
              !currentId || activeMessages.length < 2
                ? "Send at least one message first"
                : "Summarize and archive this session"
            }
          >
            <Archive className="size-3.5" />
            End &amp; Save
          </button>
        </div>
      </div>

      {/* Scroll area — was flex-1 inside the dropped flex column; the
        * empty state's natural placement (`mt-12 text-center`) handles
        * its own vertical positioning without the stretch. */}
      <div>
        {/* Closed sessions, oldest first */}
        {closedHistories.data
          ?.slice()
          .reverse()
          .map(({ session, messages }) => (
            <div key={session.thread_id}>
              <SessionDivider
                session={session}
                variant="archived"
                onDelete={deleteArchivedSession}
              />
              <div className="space-y-3">
                {renderWithDayDividers(visible(messages))}
              </div>
            </div>
          ))}

        {/* Active session divider */}
        {(closedSessions.length > 0 || activeMessages.length > 0) && (
          <SessionDivider session={null} variant="active" />
        )}

        {/* Active messages */}
        <div className="space-y-3">
          {renderWithDayDividers(visible(activeMessages))}

          {/* Live streaming turn: optimistic user bubble + accumulating
            * AI bubble. Renders only while a chat turn is in flight
            * (SSE stream open). On stream close, refreshAll() pulls
            * the canonical messages and this block clears. */}
          {streamingTurn && (
            <>
              <MessageBubble
                message={{ role: "human", content: streamingTurn.userMessage }}
              />
              <MessageBubble
                message={{
                  role: "ai",
                  content: streamingTurn.aiContent || "…",
                }}
              />
            </>
          )}
        </div>

        {/* Empty state */}
        {closedSessions.length === 0 && activeMessages.length === 0 && !streamingTurn && hydrated && (
          <div className="mt-12 text-center text-sm text-muted-foreground">
            <p>No prior conversations yet.</p>
            <p className="mt-1">Pick an action above or just ask something.</p>
          </div>
        )}

        {/* Pending spinner — hidden during streaming since the live
          * AI bubble already conveys "in progress". Stays for actions
          * (which don't stream) and pre-first-token of chat. */}
        {pending && !streamingTurn && (
          <div className="my-4 flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" />
            {pending === "chat"
              ? "Coach is thinking…"
              : pending === "summarize_and_archive"
                ? "Archiving…"
                : "Running action…"}
          </div>
        )}

        {/* Error */}
        {errorMsg && (
          <div className="my-3 rounded-md border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-700 dark:text-rose-300">
            {errorMsg}
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Sticky input — bottom-anchored above the nav */}
      <div
        className="sticky bottom-0 -mx-5 mt-4 border-t border-border bg-background/95 px-5 pb-1.5 pt-2 backdrop-blur-md sm:-mx-8 sm:px-8"
        style={{ paddingBottom: "max(env(safe-area-inset-bottom), 6px)" }}
      >
        <ChatInput onSubmit={sendChat} disabled={pending !== null} />
      </div>

      {/* Toast */}
      {archiveToast && (
        <div className="fixed bottom-24 left-1/2 z-50 max-w-[90vw] -translate-x-1/2 rounded-lg border border-border bg-foreground px-4 py-2.5 text-sm text-background shadow-lg">
          <div className="flex items-start gap-2">
            <Archive className="mt-0.5 size-4 shrink-0" />
            <pre className="whitespace-pre-wrap font-sans">{archiveToast}</pre>
          </div>
        </div>
      )}
    </div>
  );
}
