"use client";

import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Loader2, Trash2, X } from "lucide-react";
import { apiDelete, apiPost, apiPut } from "@/lib/api";
import type { PlannedWorkout, PlannedWorkoutType } from "@/lib/types";
import { cn } from "@/lib/utils";

// Edit/create modal for planned workouts (PR P4b).
//
// Two modes (single component, just controlled by `mode` prop):
//   • "create" — POST /api/planned-workouts. `initial` is null;
//     defaults: `date` = today, `type` = "easy", everything else
//     empty.
//   • "edit"  — PUT  /api/planned-workouts/{id}. `initial` pre-fills.
//
// The form is the same in both modes. Save uses upsert semantics on
// the backend: only the fields the user actually changed get sent
// (PUT), or the whole shape on POST. The backend dual-writes Google
// Cal on both paths.
//
// Delete is only shown in edit mode (you can't delete what hasn't
// been created). Confirmation gate is intentionally light — single-
// user dev app, undo is just "ask the coach to re-plan it".

const TYPES: PlannedWorkoutType[] = [
  "easy",
  "tempo",
  "interval",
  "long",
  "run",
  "swim",
  "gym",
  "other",
];

interface EditWorkoutModalProps {
  mode: "create" | "edit";
  initial: PlannedWorkout | null;
  defaultDate: string;
  // Bounds for the date picker — matches the parent card's 14-day
  // window. Without these, saving for a date outside the window
  // makes the row silently disappear from the list (it's still
  // stored, but the UpcomingWorkouts query won't pull it).
  minDate: string;
  maxDate: string;
  onClose: () => void;
  onSaved: () => void;
}

interface FormState {
  date: string;
  type: PlannedWorkoutType;
  target_pace_min_mi: string;
  target_hr: string;
  distance_mi: string;
  duration_min: string;
  notes: string;
}

function toForm(w: PlannedWorkout | null, defaultDate: string): FormState {
  return {
    date: w?.date ?? defaultDate,
    type: (w?.type as PlannedWorkoutType) ?? "easy",
    target_pace_min_mi:
      w?.target_pace_min_mi != null ? String(w.target_pace_min_mi) : "",
    target_hr: w?.target_hr != null ? String(w.target_hr) : "",
    distance_mi: w?.distance_mi != null ? String(w.distance_mi) : "",
    duration_min: w?.duration_min != null ? String(w.duration_min) : "",
    notes: w?.notes ?? "",
  };
}

export function EditWorkoutModal({
  mode,
  initial,
  defaultDate,
  minDate,
  maxDate,
  onClose,
  onSaved,
}: EditWorkoutModalProps) {
  const [form, setForm] = useState<FormState>(() => toForm(initial, defaultDate));

  // Close on Escape — small but expected, and the alternative
  // (clicking outside the panel) doesn't work well on mobile.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Parse a target-pace input that may be either decimal min/mi
  // ("7.5") or mm:ss ("7:30"). Runners naturally read pace as mm:ss
  // so the UI accepts both. Returns null on unparseable input — the
  // caller treats null the same as empty for "drop this field" /
  // "clear this field" semantics.
  function parsePace(raw: string): number | null {
    const s = raw.trim();
    if (s === "") return null;
    const colon = s.indexOf(":");
    if (colon !== -1) {
      const m = Number(s.slice(0, colon));
      const sec = Number(s.slice(colon + 1));
      if (
        !Number.isFinite(m) || !Number.isFinite(sec) ||
        m < 0 || sec < 0 || sec >= 60
      ) {
        return null;
      }
      return m + sec / 60;
    }
    const n = Number(s);
    return Number.isFinite(n) && n >= 0 ? n : null;
  }

  function buildBody(): Record<string, unknown> {
    // Numeric optional fields: empty string → null (clear-on-save
    // semantics on the backend's optional fields). Required fields
    // (date + type) always present.
    const out: Record<string, unknown> = {
      date: form.date,
      type: form.type,
    };
    // target_pace_min_mi handled separately — it accepts mm:ss
    // syntax alongside decimal. Other numerics are plain non-
    // negative numbers.
    const plainNumeric = ["target_hr", "distance_mi", "duration_min"] as const;
    for (const k of plainNumeric) {
      const raw = form[k].trim();
      if (raw === "") {
        // On edit, send null to explicitly clear. On create, just
        // omit so we don't ship null over the validation (matches
        // P4a's create endpoint which uses exclude_none=True).
        if (mode === "edit") out[k] = null;
      } else {
        const n = Number(raw);
        // Reject negatives — backend now enforces this too but
        // client-side gate gives instant feedback via the
        // disabled-Save state below.
        if (Number.isFinite(n) && n >= 0) out[k] = n;
      }
    }
    // Pace: try mm:ss first, fall back to decimal. parsePace
    // collapses "7:30" → 7.5 client-side so the backend only ever
    // sees the canonical decimal form.
    const paceRaw = form.target_pace_min_mi.trim();
    if (paceRaw === "") {
      if (mode === "edit") out.target_pace_min_mi = null;
    } else {
      const parsed = parsePace(paceRaw);
      if (parsed !== null) out.target_pace_min_mi = parsed;
    }
    // Notes ALWAYS sent (even empty) so users can clear a saved
    // note — same pattern as TodaysCheckin (codex P2 lesson from #80).
    out.notes = form.notes.trim();
    return out;
  }

  // Block save when pace input is non-empty but unparseable — the
  // silent-clear case. Without this, typing "7:30:foo" in pace and
  // hitting Save would clear a previously-pinned target.
  const paceParseError =
    form.target_pace_min_mi.trim() !== "" &&
    parsePace(form.target_pace_min_mi) === null;

  const save = useMutation({
    mutationFn: () => {
      const body = buildBody();
      if (mode === "create") {
        return apiPost("/api/planned-workouts", body);
      } else {
        return apiPut(`/api/planned-workouts/${initial!.id}`, body);
      }
    },
    onSuccess: () => onSaved(),
  });

  const del = useMutation({
    mutationFn: () => apiDelete(`/api/planned-workouts/${initial!.id}`),
    onSuccess: () => onSaved(),
  });

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 backdrop-blur-sm sm:items-center"
      onClick={(e) => {
        // Click on backdrop (not the panel) closes the modal.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="max-h-[90vh] w-full max-w-md overflow-y-auto rounded-t-xl bg-card p-5 shadow-xl sm:rounded-xl sm:p-6">
        <div className="mb-4 flex items-start justify-between">
          <div>
            <h2 className="font-heading text-lg font-semibold">
              {mode === "create" ? "Schedule workout" : "Edit workout"}
            </h2>
            <p className="text-xs text-muted-foreground">
              {mode === "create"
                ? "Lands on your Google Calendar (silent — no notification)."
                : "Edits sync back to Google Calendar automatically."}
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
          <Field label="Date">
            <input
              type="date"
              value={form.date}
              min={minDate}
              max={maxDate}
              onChange={(e) => setForm((f) => ({ ...f, date: e.target.value }))}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40"
            />
          </Field>

          <Field label="Type">
            <div className="flex flex-wrap gap-1.5">
              {TYPES.map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setForm((f) => ({ ...f, type: t }))}
                  className={cn(
                    "rounded-full border px-2.5 py-1 text-xs font-medium capitalize transition-colors",
                    form.type === t
                      ? "border-foreground bg-foreground text-background"
                      : "border-border bg-background text-muted-foreground hover:bg-muted/40",
                  )}
                  aria-pressed={form.type === t}
                >
                  {t}
                </button>
              ))}
            </div>
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Distance (mi)">
              <NumInput
                value={form.distance_mi}
                onChange={(v) => setForm((f) => ({ ...f, distance_mi: v }))}
                placeholder="—"
                step="0.1"
              />
            </Field>
            <Field label="Duration (min)">
              <NumInput
                value={form.duration_min}
                onChange={(v) => setForm((f) => ({ ...f, duration_min: v }))}
                placeholder="—"
                step="1"
              />
            </Field>
            {/* Pace: accept either decimal min/mi or mm:ss. type=text
                rather than number so the colon survives — runners
                read pace as "7:30/mi", and `<input type="number">`
                would either strip the colon (Chrome) or quietly
                produce an empty value (Firefox), which buildBody
                then maps to "clear this field" → silent data loss
                on edit. Parse client-side back to decimal in
                parsePace(). */}
            <Field label="Target pace (mm:ss or min/mi)">
              <input
                type="text"
                inputMode="decimal"
                value={form.target_pace_min_mi}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    target_pace_min_mi: e.target.value,
                  }))
                }
                placeholder="7:30  or  7.5"
                aria-invalid={paceParseError}
                className={cn(
                  "w-full rounded-md border bg-background px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40",
                  paceParseError
                    ? "border-rose-500"
                    : "border-border",
                )}
              />
              {paceParseError && (
                <p className="mt-1 text-[10px] text-rose-600 dark:text-rose-400">
                  Use mm:ss (7:30) or decimal min/mi (7.5).
                </p>
              )}
            </Field>
            <Field label="Target HR (bpm)">
              <NumInput
                value={form.target_hr}
                onChange={(v) => setForm((f) => ({ ...f, target_hr: v }))}
                placeholder="—"
                step="1"
              />
            </Field>
          </div>

          <Field label="Notes">
            <textarea
              value={form.notes}
              onChange={(e) =>
                setForm((f) => ({ ...f, notes: e.target.value }))
              }
              placeholder="workout description, cues…"
              rows={3}
              className="w-full resize-none rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40"
            />
          </Field>
        </div>

        {(save.isError || del.isError) && (
          <p className="mt-3 text-xs text-rose-600 dark:text-rose-400">
            {/* Show BOTH errors when both fired (the race the
                disabled-state below now prevents — but defensive in
                case the guard ever regresses). */}
            {[
              (save.error as Error | null)?.message,
              (del.error as Error | null)?.message,
            ]
              .filter(Boolean)
              .join(" · ")}
          </p>
        )}

        <div className="mt-5 flex items-center gap-2">
          {mode === "edit" && (
            <button
              type="button"
              onClick={() => del.mutate()}
              // Disable when EITHER mutation is in flight. Without
              // this, tapping Delete then Save (or vice-versa) races:
              // PUT lands on a deleted row, or DELETE wins after PUT
              // and orphans the cal_event_id on disk.
              disabled={del.isPending || save.isPending}
              className="inline-flex items-center gap-1.5 rounded-full border border-rose-500/40 px-3 py-1.5 text-xs font-medium text-rose-600 transition-colors hover:bg-rose-500/10 disabled:opacity-50 dark:text-rose-400"
            >
              {del.isPending ? (
                <Loader2 className="size-3 animate-spin" />
              ) : (
                <Trash2 className="size-3" />
              )}
              Delete
            </button>
          )}
          <div className="flex-1" />
          <button
            type="button"
            onClick={onClose}
            className="rounded-full border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted/40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => save.mutate()}
            // Mirror of the Delete-button guard above: block Save
            // when EITHER mutation is in flight, plus when pace is
            // unparseable (would silently clear a stored target).
            disabled={save.isPending || del.isPending || paceParseError}
            className="inline-flex items-center gap-1.5 rounded-full bg-foreground px-3.5 py-1.5 text-xs font-medium text-background transition-opacity disabled:opacity-50"
          >
            {save.isPending && <Loader2 className="size-3 animate-spin" />}
            {mode === "create" ? "Save" : "Update"}
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

function NumInput({
  value,
  onChange,
  placeholder,
  step,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  step?: string;
}) {
  return (
    <input
      type="number"
      inputMode="decimal"
      min="0"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      step={step}
      className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40"
    />
  );
}
