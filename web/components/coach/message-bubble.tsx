"use client";

import { useState } from "react";
import { Check, Copy } from "lucide-react";
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
