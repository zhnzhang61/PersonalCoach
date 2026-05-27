"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pencil } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet, apiPost } from "@/lib/api";
import type { CheckinsResponse, DailyCheckin } from "@/lib/types";
import { cn } from "@/lib/utils";

// Today's check-in card (PR P3 — perceived layer §2).
//
// Mounted at the top of the Health tab. Two modes:
//
//   Empty (no row for today's date) — show 4 ordinal scales the user
//   taps (1-5 buttons each) + a free-text notes field. Save button
//   POSTs to /api/checkins and switches to filled mode.
//
//   Filled — show compact summary chips for whatever the user filled
//   in (skipped fields just don't render their chip). Edit pencil
//   flips back to the empty/editing mode so same-day re-submission
//   overrides the row (upsert semantics on the backend).
//
// "Today" is computed locally (user's timezone). The server stores
// YYYY-MM-DD strings so date math stays simple.

const SCALE_FIELDS: Array<{
  key: keyof Pick<DailyCheckin, "sleep_quality" | "soreness" | "mood" | "motivation">;
  label: string;
  hint: string; // "1 = worst, 5 = best" or "1 = none, 5 = max"
}> = [
  { key: "sleep_quality", label: "Sleep quality", hint: "1 = bad · 5 = great" },
  { key: "soreness", label: "Soreness", hint: "0 = none · 5 = very sore" },
  { key: "mood", label: "Mood", hint: "1 = flat · 5 = great" },
  { key: "motivation", label: "Motivation", hint: "1 = drained · 5 = fired up" },
];

function todayLocal(): string {
  // Local-tz YYYY-MM-DD. Server stores this raw, so calendar boundaries
  // match the user's experience (a check-in at 11 PM stays "today",
  // not "tomorrow" if the machine is UTC).
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

export function TodaysCheckin() {
  const today = todayLocal();

  // Last 7 days of check-ins so we can spot today's row + later we can
  // surface trend hints ("you've slept 4+ for 3 nights running").
  const query = useQuery({
    queryKey: ["checkins", 7],
    queryFn: () => apiGet<CheckinsResponse>("/api/checkins?days=7"),
  });

  if (query.isLoading) {
    return (
      <Card>
        <CardContent className="p-5">
          <Skeleton className="h-24 w-full" />
        </CardContent>
      </Card>
    );
  }

  const todaysRow = query.data?.checkins.find((c) => c.date === today) ?? null;

  // Splitting the editor into its own component lets us key it on
  // `todaysRow?.updated_at` so saving a row remounts the editor with
  // fresh draft state from the new row — no setState-in-effect needed.
  // This is the React-recommended "reset state when prop changes"
  // pattern (https://react.dev/learn/you-might-not-need-an-effect).
  return (
    <CheckinCard
      key={todaysRow?.updated_at ?? "empty"}
      today={today}
      todaysRow={todaysRow}
    />
  );
}

interface CheckinCardProps {
  today: string;
  todaysRow: DailyCheckin | null;
}

function CheckinCard({ today, todaysRow }: CheckinCardProps) {
  const qc = useQueryClient();

  // `editing` = "show the sliders". Empty state shows them by default;
  // filled state shows the summary and only opens sliders when user
  // clicks Edit. Defaults derived from `todaysRow` at mount (key prop
  // on parent ensures re-mount when the row changes).
  const [editing, setEditing] = useState(!todaysRow);

  const [draft, setDraft] = useState(() => ({
    sleep_quality: todaysRow?.sleep_quality ?? null,
    soreness: todaysRow?.soreness ?? null,
    mood: todaysRow?.mood ?? null,
    motivation: todaysRow?.motivation ?? null,
    notes: todaysRow?.notes ?? "",
  }));

  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = { date: today };
      // Scale fields: only send if the user has selected a value
      // (null = "didn't capture"). Existing rows keep their old
      // values for sliders the user didn't touch this turn, via
      // upsert_checkin's field-level merge.
      for (const f of SCALE_FIELDS) {
        if (draft[f.key] !== null) body[f.key] = draft[f.key];
      }
      // Notes: ALWAYS send (even empty). Codex P2 catch on PR #80 —
      // omitting empty notes meant "user cleared their saved note"
      // got silently merged back to the previous value on disk,
      // making cleared notes reappear after the query refetched.
      // Empty string is the canonical "clear" signal; backend's
      // _validate_checkin_fields stores `""` and the FilledSummary
      // treats falsy notes as "nothing to render", so the round-trip
      // is clean.
      body.notes = draft.notes.trim();
      return apiPost<{ ok: boolean; checkin: DailyCheckin }>(
        "/api/checkins",
        body,
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["checkins"] });
      setEditing(false);
    },
  });

  const showEditor = !todaysRow || editing;

  return (
    <Card>
      <CardContent className="space-y-4 p-5 sm:p-6">
        <div className="flex items-center justify-between gap-2">
          <div>
            <div className="eyebrow">Today&rsquo;s check-in</div>
            <h2 className="font-heading text-lg font-semibold tracking-tight sm:text-xl">
              {todaysRow ? "How you felt today" : "How are you today?"}
            </h2>
          </div>
          {todaysRow && !editing && (
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="inline-flex items-center gap-1 rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted/40"
              aria-label="Edit today's check-in"
            >
              <Pencil className="size-3" />
              Edit
            </button>
          )}
        </div>

        {showEditor ? (
          <div className="space-y-3">
            {SCALE_FIELDS.map((f) => (
              <ScaleRow
                key={f.key}
                label={f.label}
                hint={f.hint}
                // soreness is the only field that starts at 0; the
                // others start at 1. Visually they all share the
                // same 5-button row — the 0 button only renders for
                // soreness.
                min={f.key === "soreness" ? 0 : 1}
                max={5}
                value={draft[f.key]}
                onChange={(v) =>
                  setDraft((prev) => ({ ...prev, [f.key]: v }))
                }
              />
            ))}
            <div>
              <label
                htmlFor="checkin-notes"
                className="mb-1 block text-xs font-medium text-muted-foreground"
              >
                Notes (optional)
              </label>
              <textarea
                id="checkin-notes"
                value={draft.notes}
                onChange={(e) =>
                  setDraft((prev) => ({ ...prev, notes: e.target.value }))
                }
                placeholder="anything else worth noting…"
                rows={2}
                className="w-full resize-none rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40"
              />
            </div>
            <div className="flex items-center justify-end gap-2 pt-1">
              {todaysRow && editing && (
                <button
                  type="button"
                  onClick={() => setEditing(false)}
                  className="rounded-full border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted/40"
                >
                  Cancel
                </button>
              )}
              <button
                type="button"
                onClick={() => save.mutate()}
                disabled={save.isPending}
                className="inline-flex items-center gap-1.5 rounded-full bg-foreground px-3.5 py-1.5 text-xs font-medium text-background transition-opacity disabled:opacity-50"
              >
                {save.isPending && <Loader2 className="size-3 animate-spin" />}
                {todaysRow ? "Update" : "Save"}
              </button>
            </div>
            {save.isError && (
              <p className="text-xs text-rose-600 dark:text-rose-400">
                Save failed: {(save.error as Error).message}
              </p>
            )}
          </div>
        ) : (
          <FilledSummary row={todaysRow!} />
        )}
      </CardContent>
    </Card>
  );
}

interface ScaleRowProps {
  label: string;
  hint: string;
  min: number;
  max: number;
  value: number | null;
  onChange: (v: number) => void;
}

function ScaleRow({ label, hint, min, max, value, onChange }: ScaleRowProps) {
  const buttons: number[] = [];
  for (let n = min; n <= max; n++) buttons.push(n);
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-sm font-medium">{label}</span>
        <span className="text-[10px] text-muted-foreground">{hint}</span>
      </div>
      <div className="flex gap-1.5">
        {buttons.map((n) => (
          <button
            key={n}
            type="button"
            onClick={() => onChange(n)}
            className={cn(
              "flex-1 rounded-md border py-2 text-sm font-medium transition-colors",
              value === n
                ? "border-foreground bg-foreground text-background"
                : "border-border bg-background text-muted-foreground hover:bg-muted/40",
            )}
            aria-pressed={value === n}
          >
            {n}
          </button>
        ))}
      </div>
    </div>
  );
}

function FilledSummary({ row }: { row: DailyCheckin }) {
  // Chip per filled-in scale. Skipped scales (null) simply don't render
  // — partial check-ins are valid (the user may have only filled mood
  // and skipped the others).
  const chips: { label: string; value: number; key: string }[] = [];
  for (const f of SCALE_FIELDS) {
    const v = row[f.key];
    if (v != null) chips.push({ label: f.label, value: v, key: f.key });
  }
  if (chips.length === 0 && !row.notes) {
    return (
      <p className="text-sm text-muted-foreground">
        Empty check-in — edit to fill anything in.
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {chips.map((c) => (
          <span
            key={c.key}
            className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/30 px-2.5 py-1 text-xs"
          >
            <span className="text-muted-foreground">{c.label}</span>
            <span className="font-semibold">{c.value}</span>
          </span>
        ))}
      </div>
      {row.notes && (
        <p className="text-sm text-foreground/80">{row.notes}</p>
      )}
    </div>
  );
}
