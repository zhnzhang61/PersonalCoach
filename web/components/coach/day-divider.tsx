// Day-boundary divider rendered WITHIN a session's message stream.
// Distinct from SessionDivider — that one separates sessions
// (archived / active). DayDivider separates calendar days within a
// single session that spans multiple days. Without it, a long-running
// session like coach_20260511T150040Z (5/11 → 5/14, 23 messages) reads
// as a flat wall with no temporal anchor.
//
// Visually subtler than SessionDivider: thinner dashed line, smaller
// label, no metadata pills. The label is local-date short form like
// "Tue · May 14" — short enough to fit inline, anchored enough to
// orient the user.

interface Props {
  /** ISO timestamp of the FIRST message on this day. We format it
   * locally so users see their own timezone. */
  iso: string;
}

function fmtDay(iso: string): string {
  const d = new Date(iso);
  // "Tue · May 14" — weekday gives quick recall ("oh, that was Tuesday")
  // and date disambiguates across weeks. We deliberately drop the year
  // to keep the label tight; sessions rarely span multiple years and
  // the SessionDivider above shows the start date anyway.
  const weekday = d.toLocaleDateString("en", { weekday: "short" });
  const monthDay = d.toLocaleDateString("en", {
    month: "short",
    day: "numeric",
  });
  return `${weekday} · ${monthDay}`;
}

export function DayDivider({ iso }: Props) {
  return (
    <div className="my-3 flex items-center gap-2 px-1" aria-hidden>
      <div className="h-px flex-1 border-t border-dotted border-border/60" />
      <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
        {fmtDay(iso)}
      </span>
      <div className="h-px flex-1 border-t border-dotted border-border/60" />
    </div>
  );
}
