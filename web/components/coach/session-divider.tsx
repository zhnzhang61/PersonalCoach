import { Archive, Sparkles, Trash2 } from "lucide-react";
import type { CoachSession } from "@/lib/types";

interface Props {
  session: CoachSession | null; // null = the divider above the active session
  variant: "archived" | "active";
  // Optional: render a small trash icon on archived sessions that
  // calls this when clicked. The parent owns the actual delete request
  // so it can confirm and refresh queries.
  onDelete?: (thread_id: string) => void;
}

/** "May 10 · 6:20 PM" — date plus local-time HH:MM, so the user can
 * tell at a glance which session the bubble below belongs to without
 * having to remember exact timestamps. */
function fmtDateTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const date = d.toLocaleDateString("en", { month: "short", day: "numeric" });
  const time = d.toLocaleTimeString("en", {
    hour: "numeric",
    minute: "2-digit",
  });
  return `${date} · ${time}`;
}

// Dashed divider between sessions. For closed sessions: shows date+time,
// summary, and small tags for new topics/episodes. The most recent
// divider before the live messages is "active" — bare separator with
// the "current session" label.
export function SessionDivider({ session, variant, onDelete }: Props) {
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
  const dateLabel = fmtDateTime(session.closed_at);
  return (
    <div className="my-5 px-1">
      <div className="flex items-center gap-2">
        <div className="h-px flex-1 border-t border-dashed border-border" />
        <span className="inline-flex items-center gap-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          <Archive className="size-3" />
          archived {dateLabel}
          {onDelete ? (
            <button
              type="button"
              onClick={() => onDelete(session.thread_id)}
              className="ml-1 inline-flex size-5 items-center justify-center rounded text-muted-foreground/70 transition-colors hover:bg-rose-500/10 hover:text-rose-600 dark:hover:text-rose-400"
              title="Delete this archived session"
              aria-label={`Delete archived session ${dateLabel}`}
            >
              <Trash2 className="size-3" />
            </button>
          ) : null}
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
