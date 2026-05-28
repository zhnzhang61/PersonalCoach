"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarRange, Loader2, Plus, Trash2, X } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  apiDelete,
  apiGet,
  apiPost,
} from "@/lib/api";
import type {
  ExternalEvent,
  ExternalEventType,
  ExternalEventsResponse,
} from "@/lib/types";
import { cn } from "@/lib/utils";

// Window for what shows in the card AND the modal's date-picker
// bounds. Context events span longer windows than planned workouts
// (a 30-day-old illness still shapes today's read; a trip 2 months
// out is worth logging now) — wider than the 14-day planned-workout
// window. Symmetric ±60 keeps the modal bounds the same as the list
// window so a saved event never silently disappears from view.
const WINDOW_DAYS_BACK = 60;
const WINDOW_DAYS_FORWARD = 60;

// External-context events card (PR P5 — external context §4).
//
// Lives on the Health tab. Lists user-logged travel / illness /
// life_stress events overlapping a 4-week window (2 weeks back +
// 2 weeks forward — illness from last week is still relevant
// context for *this* week's runs).
//
// Quick-add UX:
//   • "+ Log event" → modal with type + start_date + end_date +
//     description. Save → POST /api/memory/external-events.
//   • Each existing event has a small trash icon → DELETE.
//
// We don't surface edit-in-place for these (unlike planned workouts)
// — they're descriptive context, not parameter sets. If a user got
// the dates wrong, deleting + re-logging is fine.

const TYPE_LABEL: Record<ExternalEventType, string> = {
  travel: "Travel",
  illness: "Illness",
  life_stress: "Life stress",
};

const TYPE_COLOR: Record<ExternalEventType, string> = {
  // Travel = blue (timezone shift / route change reads "navigational").
  travel: "border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300",
  // Illness = rose (clinical / cautionary).
  illness: "border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300",
  // Life stress = amber (heads-up but not alarm).
  life_stress: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
};

function todayLocal(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function plusDays(iso: string, days: number): string {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function ExternalEvents() {
  const today = todayLocal();
  const start = plusDays(today, -WINDOW_DAYS_BACK);
  const end = plusDays(today, WINDOW_DAYS_FORWARD);

  const query = useQuery({
    queryKey: ["external-events", start, end],
    queryFn: () =>
      apiGet<ExternalEventsResponse>(
        `/api/memory/external-events?start=${start}&end=${end}`,
      ),
    staleTime: 30_000,
  });

  const [adding, setAdding] = useState(false);

  const events = useMemo(() => query.data?.events ?? [], [query.data]);

  return (
    <>
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-2 space-y-0">
          <div>
            <div className="flex items-center gap-2">
              <CalendarRange className="size-4 text-muted-foreground" />
              <CardTitle className="text-base">Context events</CardTitle>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              Travel, illness, life stress — things that change what
              your HR/HRV means.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border bg-background px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted/40"
          >
            <Plus className="size-3" />
            Log
          </button>
        </CardHeader>
        <CardContent>
          {query.isLoading && !query.data ? (
            <Skeleton className="h-16 w-full" />
          ) : query.isError ? (
            <p className="py-3 text-center text-sm text-rose-600 dark:text-rose-400">
              Couldn&rsquo;t load events —{" "}
              {(query.error as Error | null)?.message ?? "please retry."}
            </p>
          ) : events.length === 0 ? (
            <p className="py-3 text-center text-sm text-muted-foreground">
              No logged context events. Tap{" "}
              <span className="font-medium">Log</span> to add travel,
              illness, or stress windows.
            </p>
          ) : (
            <ul className="space-y-2">
              {events.map((ev) => (
                <EventRow key={ev.episode_id} event={ev} />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {adding && (
        <AddEventModal
          today={today}
          minDate={start}
          maxDate={end}
          onClose={() => setAdding(false)}
        />
      )}
    </>
  );
}

function EventRow({ event }: { event: ExternalEvent }) {
  const qc = useQueryClient();
  const description =
    (event.context.description as string | undefined) ??
    event.lesson_learned ??
    "";
  const sameDay = event.start_date === event.end_date;

  const del = useMutation({
    mutationFn: () =>
      apiDelete(`/api/memory/external-events/${event.episode_id}`),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["external-events"] }),
    // No onError → before this, a 500 / network blip silently
    // stopped the spinner and left the row mounted with no signal.
    // Surface the failure below the row so the user knows to retry.
  });

  return (
    <li className="rounded-md border border-border bg-background p-3">
      <div className="flex items-start gap-3">
        <span
          className={cn(
            "shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide",
            TYPE_COLOR[event.event_type],
          )}
        >
          {TYPE_LABEL[event.event_type]}
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          <div className="text-xs text-muted-foreground">
            {sameDay
              ? formatDateRange(event.start_date, event.start_date, true)
              : formatDateRange(event.start_date, event.end_date, false)}
          </div>
          {description && (
            <p className="text-sm text-foreground/90">{description}</p>
          )}
        </div>
        <button
          type="button"
          onClick={() => del.mutate()}
          disabled={del.isPending}
          className="shrink-0 rounded-full p-1 text-muted-foreground transition-colors hover:bg-rose-500/10 hover:text-rose-600 disabled:opacity-50"
          aria-label="Delete event"
        >
          {del.isPending ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Trash2 className="size-3.5" />
          )}
        </button>
      </div>
      {del.isError && (
        <p className="mt-2 text-[10px] text-rose-600 dark:text-rose-400">
          Delete failed —{" "}
          {(del.error as Error | null)?.message ?? "please retry."}
        </p>
      )}
    </li>
  );
}

function formatDateRange(
  start: string,
  end: string,
  sameDay: boolean,
): string {
  const fmt = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
    });
  if (sameDay) return fmt(start);
  return `${fmt(start)} → ${fmt(end)}`;
}

interface AddEventModalProps {
  today: string;
  minDate: string;
  maxDate: string;
  onClose: () => void;
}

function AddEventModal({
  today,
  minDate,
  maxDate,
  onClose,
}: AddEventModalProps) {
  const qc = useQueryClient();
  const [eventType, setEventType] = useState<ExternalEventType>("travel");
  const [startDate, setStartDate] = useState(today);
  const [endDate, setEndDate] = useState(today);
  const [description, setDescription] = useState("");

  // Cancel-then-reopen race: if the user clicks Cancel (or backdrop)
  // while a save is in flight, the v1 modal unmounts but the
  // mutation's onSuccess callback still fires from v1's closure when
  // the HTTP request resolves. That captured `onClose` would close
  // v2 — losing whatever the user typed there. Guard onSuccess /
  // invalidate with a mount check so unmounted modals are inert.
  const isMounted = useRef(true);
  useEffect(() => {
    return () => {
      isMounted.current = false;
    };
  }, []);

  const create = useMutation({
    mutationFn: () =>
      apiPost("/api/memory/external-events", {
        event_type: eventType,
        start_date: startDate,
        end_date: endDate,
        description: description.trim(),
      }),
    onSuccess: () => {
      // Even if this v1 instance unmounted before the request
      // resolved, the query invalidation is harmless to fire — it
      // just refetches. But onClose would close whatever v2 modal
      // is currently mounted, so gate that.
      qc.invalidateQueries({ queryKey: ["external-events"] });
      if (isMounted.current) onClose();
    },
  });

  // Block save on obvious bad input — backend also enforces, but
  // client-side gating keeps the disabled state honest.
  const descMissing = description.trim() === "";
  const dateRangeBad = endDate < startDate;
  const blocked = descMissing || dateRangeBad;

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 backdrop-blur-sm sm:items-center"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="max-h-[90vh] w-full max-w-md overflow-y-auto rounded-t-xl bg-card p-5 shadow-xl sm:rounded-xl sm:p-6">
        <div className="mb-4 flex items-start justify-between">
          <div>
            <h2 className="font-heading text-lg font-semibold">
              Log context event
            </h2>
            <p className="text-xs text-muted-foreground">
              The coach sees this when reading your numbers.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full p-1 text-muted-foreground transition-colors hover:bg-muted/40"
            aria-label="Close"
          >
            <X className="size-4" />
          </button>
        </div>

        <div className="space-y-3">
          <Field label="Type">
            <div className="flex flex-wrap gap-1.5">
              {(["travel", "illness", "life_stress"] as const).map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setEventType(t)}
                  className={cn(
                    "rounded-full border px-2.5 py-1 text-xs font-medium capitalize transition-colors",
                    eventType === t
                      ? "border-foreground bg-foreground text-background"
                      : "border-border bg-background text-muted-foreground hover:bg-muted/40",
                  )}
                  aria-pressed={eventType === t}
                >
                  {TYPE_LABEL[t]}
                </button>
              ))}
            </div>
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Start">
              <input
                type="date"
                value={startDate}
                min={minDate}
                max={maxDate}
                onChange={(e) => setStartDate(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40"
              />
            </Field>
            <Field label="End">
              <input
                type="date"
                value={endDate}
                // min is the LATER of (window start, user's chosen
                // start) so the picker enforces both "inside window"
                // and "after start" simultaneously.
                min={startDate > minDate ? startDate : minDate}
                max={maxDate}
                onChange={(e) => setEndDate(e.target.value)}
                className={cn(
                  "w-full rounded-md border bg-background px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40",
                  dateRangeBad ? "border-rose-500" : "border-border",
                )}
              />
            </Field>
          </div>
          {dateRangeBad && (
            <p className="text-[10px] text-rose-600 dark:text-rose-400">
              End must be on or after start.
            </p>
          )}

          <Field label="Description">
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={
                eventType === "travel"
                  ? "Flew to Tokyo, 13h ahead"
                  : eventType === "illness"
                    ? "Stomach bug, low energy"
                    : "Demo prep crunch week"
              }
              rows={3}
              className="w-full resize-none rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40"
            />
          </Field>
        </div>

        {create.isError && (
          <p className="mt-3 text-xs text-rose-600 dark:text-rose-400">
            {(create.error as Error | null)?.message ?? "Save failed."}
          </p>
        )}

        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-full border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted/40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => create.mutate()}
            disabled={create.isPending || blocked}
            className="inline-flex items-center gap-1.5 rounded-full bg-foreground px-3.5 py-1.5 text-xs font-medium text-background transition-opacity disabled:opacity-50"
          >
            {create.isPending && <Loader2 className="size-3 animate-spin" />}
            Save
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-muted-foreground">
        {label}
      </span>
      {children}
    </label>
  );
}
