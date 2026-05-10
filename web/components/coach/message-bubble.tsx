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
export function MessageBubble({ message }: Props) {
  const isUser = message.role === "human";
  return (
    <div
      className={cn(
        "flex w-full",
        isUser ? "justify-end" : "justify-start",
      )}
    >
      <div
        className={cn(
          "max-w-[88%] rounded-2xl px-3.5 py-2.5 text-[14px] leading-relaxed",
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
    </div>
  );
}
