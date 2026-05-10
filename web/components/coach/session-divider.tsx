import { Archive, Sparkles } from "lucide-react";
import type { CoachSession } from "@/lib/types";

interface Props {
  session: CoachSession | null; // null = the divider above the active session
  variant: "archived" | "active";
}

function fmtDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en", {
    month: "short",
    day: "numeric",
  });
}

// Dashed divider between sessions. For closed sessions: shows date,
// summary, and small tags for new topics/episodes. The most recent
// divider before the live messages is "active" — bare separator with
// the "current session" label.
export function SessionDivider({ session, variant }: Props) {
  if (variant === "active") {
    return (
      <div className="my-4 flex items-center gap-2 px-1">
        <div className="h-px flex-1 border-t border-dashed border-border" />
        <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          <Sparkles className="mr-1 inline size-3" /> current session
        </span>
        <div className="h-px flex-1 border-t border-dashed border-border" />
      </div>
    );
  }

  // Archived
  if (!session) return null;
  const dateLabel = fmtDate(session.closed_at);
  return (
    <div className="my-5 px-1">
      <div className="flex items-center gap-2">
        <div className="h-px flex-1 border-t border-dashed border-border" />
        <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          <Archive className="mr-1 inline size-3" />
          archived {dateLabel}
        </span>
        <div className="h-px flex-1 border-t border-dashed border-border" />
      </div>
      {session.summary ? (
        <p className="mx-auto mt-2 max-w-prose text-xs text-muted-foreground">
          {session.summary}
        </p>
      ) : null}
      {(session.topics_added > 0 || session.episodes_added > 0) && (
        <div className="mt-1.5 flex justify-center gap-1.5 text-[10px] text-muted-foreground">
          {session.topics_added > 0 && (
            <span className="rounded-full border border-border px-2 py-0.5">
              +{session.topics_added}{" "}
              {session.topics_added === 1 ? "topic" : "topics"}
            </span>
          )}
          {session.episodes_added > 0 && (
            <span className="rounded-full border border-border px-2 py-0.5">
              +{session.episodes_added}{" "}
              {session.episodes_added === 1 ? "episode" : "episodes"}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
