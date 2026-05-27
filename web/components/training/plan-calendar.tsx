"use client";

import { useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import FullCalendar from "@fullcalendar/react";
import dayGridPlugin from "@fullcalendar/daygrid";
import timeGridPlugin from "@fullcalendar/timegrid";
import interactionPlugin from "@fullcalendar/interaction";
import { Plug } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import type {
  CalendarEvent,
  CalendarEventsResponse,
  CalendarEventSource,
} from "@/lib/types";

type ViewMode = "month" | "day";

// FullCalendar's view names — keep our ViewMode minimal but map to its
// vocabulary at the boundary.
const FC_VIEW: Record<ViewMode, string> = {
  month: "dayGridMonth",
  day: "timeGridDay",
};

// Source-specific colors. The "source" discriminator on each event drives
// styling so the user can tell at a glance which calendar an item came
// from. Same colors used in tooltips, future legend, etc.
const SOURCE_STYLE: Record<CalendarEventSource, { bg: string; border: string; text: string }> = {
  google: { bg: "#475569", border: "#475569", text: "#ffffff" },         // slate = life event
  google_error: { bg: "#fee2e2", border: "#dc2626", text: "#991b1b" },   // red
  manual: { bg: "#fc4c02", border: "#fc4c02", text: "#ffffff" },         // strava-orange (manual activities)
  garmin_run: { bg: "#16a34a", border: "#16a34a", text: "#ffffff" },     // green = completed run
  // AI-authored planned workout (PR P4b). Distinct hue from the
  // green of completed runs so a glance answers "done vs upcoming".
  // Amber reads as "pending" in the rest of the palette.
  planned_workout: { bg: "#d97706", border: "#d97706", text: "#ffffff" },
};

export function PlanCalendar() {
  const [view, setView] = useState<ViewMode>("month");
  // FullCalendar reports the visible date window via datesSet. We feed
  // those dates back into the events query so the API only fetches what's
  // currently on screen — no point loading a year of Google events when
  // showing today.
  const [range, setRange] = useState<{ start: string; end: string } | null>(
    null,
  );
  const calRef = useRef<FullCalendar | null>(null);

  const eventsQuery = useQuery({
    queryKey: ["calendar", "events", range?.start, range?.end],
    queryFn: () =>
      apiGet<CalendarEventsResponse>(
        `/api/calendar/events?start=${encodeURIComponent(range!.start)}` +
          `&end=${encodeURIComponent(range!.end)}`,
      ),
    enabled: !!range,
    staleTime: 30_000,
  });

  const fcEvents = useMemo(() => {
    const items = eventsQuery.data?.events ?? [];
    return items
      .filter((e) => e.source !== "google_error")
      .map((e: CalendarEvent) => {
        const style = SOURCE_STYLE[e.source];
        return {
          id: e.id,
          title: e.title,
          start: e.start,
          end: e.end,
          allDay: e.all_day,
          backgroundColor: style.bg,
          borderColor: style.border,
          textColor: style.text,
          extendedProps: {
            source: e.source,
            description: e.description ?? null,
            location: e.location ?? null,
            activity_id: e.activity_id ?? null,
          },
        };
      });
  }, [eventsQuery.data]);

  const googleConnected = eventsQuery.data?.google_connected ?? null;
  const googleError = eventsQuery.data?.events.find(
    (e) => e.source === "google_error",
  );

  const onChangeView = (next: ViewMode) => {
    setView(next);
    calRef.current?.getApi().changeView(FC_VIEW[next]);
  };

  return (
    <Card>
      <CardHeader className="space-y-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base">Plan calendar</CardTitle>
          <div className="flex shrink-0 rounded-md border border-border bg-background p-0.5 text-[11px] font-medium">
            {(["month", "day"] as const).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => onChangeView(v)}
                className={
                  "rounded px-2 py-0.5 transition-colors " +
                  (view === v
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:text-foreground")
                }
                aria-pressed={view === v}
              >
                {v === "month" ? "Month" : "Day"}
              </button>
            ))}
          </div>
        </div>

        {/* Google connection state — banner above the calendar */}
        {googleConnected === false && (
          <a
            href="/oauth/google/start"
            className="flex items-center gap-2 rounded-md border border-border bg-muted/30 p-3 text-xs hover:bg-muted/50"
          >
            <Plug className="size-4 text-muted-foreground" />
            <span className="flex-1">
              Connect Google Calendar to see life events alongside your training.
            </span>
            <span className="font-medium text-foreground">Connect</span>
          </a>
        )}
        {googleError && (
          <div className="rounded-md border border-rose-500/30 bg-rose-500/10 p-2 text-xs text-rose-700 dark:text-rose-300">
            {googleError.title}
          </div>
        )}
      </CardHeader>
      <CardContent>
        {eventsQuery.isLoading && !eventsQuery.data ? (
          <Skeleton className="h-[480px] w-full" />
        ) : (
          <div className="plan-calendar -mx-2 sm:mx-0">
            <FullCalendar
              ref={calRef}
              plugins={[dayGridPlugin, timeGridPlugin, interactionPlugin]}
              initialView={FC_VIEW[view]}
              headerToolbar={{
                left: "prev,next today",
                center: "title",
                right: "",
              }}
              height={520}
              events={fcEvents}
              firstDay={1}
              nowIndicator
              datesSet={(arg) => {
                // arg.startStr / endStr are ISO. Use them as the cache key
                // so flipping months only refetches once per month.
                setRange({ start: arg.startStr, end: arg.endStr });
              }}
              dayMaxEvents={3}
              eventDisplay="block"
              displayEventEnd
              eventTimeFormat={{
                hour: "numeric",
                minute: "2-digit",
                meridiem: "short",
              }}
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
