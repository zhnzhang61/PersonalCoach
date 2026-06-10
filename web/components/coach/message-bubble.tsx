"use client";

import { useState } from "react";
import { Check, Copy, FileCheck2, FileWarning } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import type { CoachMessage } from "@/lib/types";

interface Props {
  message: CoachMessage;
}

// One chat message. AI bubbles render markdown (the agent's output is
// markdown by system-prompt contract). Human bubbles render plain text
// in a muted-accent bubble. Tool / system messages from the
// checkpointer state are dropped at the parent level — we don't show
// them.
//
// Selection + copy:
//   - `select-text` is set explicitly on the rendered text so the iOS
//     long-press → "Copy" menu fires reliably. Nothing in the project
//     sets user-select: none on this subtree, but small font + sticky
//     parents make the long-press flaky on iPhone — so on top of
//     long-press we render a small Copy button below every bubble.
//   - The button copies the RAW message.content (i.e. the agent's
//     original markdown, not the rendered HTML). For human messages
//     that's the user's literal text. For AI messages that's the
//     markdown the user is most likely pasting into a note app, where
//     headings / lists round-trip rather than turning into plain text.
export function MessageBubble({ message }: Props) {
  const isUser = message.role === "human";
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Older Safari without secure-context clipboard API: fall back
      // to a hidden textarea + execCommand. Rare on modern iOS.
      try {
        const ta = document.createElement("textarea");
        ta.value = message.content;
        ta.setAttribute("readonly", "");
        ta.style.position = "absolute";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      } catch {
        // Both paths failed — leave the icon as-is so the user can
        // still long-press the bubble to copy manually.
      }
    }
  };

  return (
    <div
      className={cn(
        "flex w-full flex-col gap-1",
        isUser ? "items-end" : "items-start",
      )}
    >
      <div
        className={cn(
          "max-w-[88%] rounded-2xl px-3.5 py-2.5 text-[14px] leading-relaxed select-text",
          isUser
            ? "bg-foreground text-background"
            : "bg-muted/40 text-foreground",
        )}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : (
          <div className="prose prose-sm dark:prose-invert max-w-none [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {message.content}
            </ReactMarkdown>
          </div>
        )}
      </div>
      {/* "档案已更新 ✓" — rendered ONLY from facts_recorded, which the
        * backend derives from record_coach_fact tool calls that actually
        * happened (checkpointed; or fact_recorded SSE events while
        * streaming). Deliberately NOT parsed from the model's text: the
        * agent once claimed "已将信息更新至你的档案" with zero tool calls
        * (2026-05-30). This badge is the source of truth for "did it
        * record" — if the model says it recorded and this badge is
        * absent, the model is lying. */}
      {!isUser && (message.facts_recorded?.length ?? 0) > 0 && (
        <div className="ml-1 flex flex-wrap items-center gap-1.5 text-[11px] leading-none text-emerald-700 dark:text-emerald-400">
          <FileCheck2 className="size-3" aria-hidden />
          <span>
            档案已更新:{" "}
            {message.facts_recorded!.map((a, i) => (
              <span key={`${a}-${i}`} className="font-mono">
                {a}
                {i < message.facts_recorded!.length - 1 ? ", " : ""}
              </span>
            ))}{" "}
            ✓
          </span>
        </div>
      )}
      {/* The durable negative twin (PR #105 review): this message
        * claimed a write, the correction round ran, and STILL no
        * successful record_coach_fact happened. Server-derived from the
        * checkpointed correction sentinel, so unlike the warning text
        * streamed into the live bubble it survives reloads — a false
        * claim can never present itself as clean in persisted history. */}
      {!isUser && message.claim_unverified && (
        <div className="ml-1 flex flex-wrap items-center gap-1.5 text-[11px] leading-none text-rose-700 dark:text-rose-400">
          <FileWarning className="size-3" aria-hidden />
          <span>系统校验：本轮未发生档案写入（该回复的「已记录」声称未经证实）</span>
        </div>
      )}
      <button
        type="button"
        onClick={copy}
        // Subtle ghost-style — always visible (mobile-first; there's no
        // hover state to hide behind) but quiet enough not to compete
        // with the message itself. Aligned with the bubble edge via the
        // parent's items-end / items-start.
        className={cn(
          "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] leading-none text-muted-foreground transition-colors hover:text-foreground active:text-foreground",
          isUser ? "mr-1" : "ml-1",
        )}
        aria-label={copied ? "Copied" : "Copy message"}
      >
        {copied ? (
          <>
            <Check className="size-3" />
            <span>copied</span>
          </>
        ) : (
          <>
            <Copy className="size-3" />
            <span>copy</span>
          </>
        )}
      </button>
    </div>
  );
}
