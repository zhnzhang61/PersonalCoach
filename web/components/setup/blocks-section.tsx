"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2, X } from "lucide-react";
import { apiDelete, apiGet, apiPost, apiPut } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  BLOCK_PRIMARY_EVENTS,
  type BlockCreateBody,
  type BlockPrimaryEvent,
  type BlocksResponse,
  type BlockUpdateBody,
  type TrainingBlock,
} from "@/lib/types";

const SELECT_CLASS =
  "w-full appearance-none rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40";

function todayIso(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function plusWeeksIso(weeks: number): string {
  const d = new Date();
  d.setDate(d.getDate() + weeks * 7);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

interface BlockFormProps {
  initial?: Partial<TrainingBlock>;
  onSubmit: (b: BlockCreateBody) => void;
  onCancel: () => void;
  saving: boolean;
  error?: string;
  submitLabel: string;
}

function BlockForm({
  initial,
  onSubmit,
  onCancel,
  saving,
  error,
  submitLabel,
}: BlockFormProps) {
  const [name, setName] = useState(initial?.name ?? "");
  const [start, setStart] = useState(initial?.start_date ?? todayIso());
  const [end, setEnd] = useState(initial?.end_date ?? plusWeeksIso(16));
  const [event, setEvent] = useState<BlockPrimaryEvent>(
    (initial?.primary_event as BlockPrimaryEvent) ?? "running",
  );

  const submit = () => {
    onSubmit({
      name: name.trim(),
      start_date: start,
      end_date: end,
      primary_event: event,
    });
  };

  return (
    <div className="space-y-3">
      <label className="flex flex-col gap-1">
        <span className="eyebrow text-[10px]">Name</span>
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Fall 2026 Marathon Build"
        />
      </label>
      <div className="grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-[10px]">Start</span>
          <Input
            type="date"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-[10px]">End</span>
          <Input
            type="date"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </label>
      </div>
      <label className="flex flex-col gap-1">
        <span className="eyebrow text-[10px]">Primary event</span>
        <select
          className={SELECT_CLASS}
          value={event}
          onChange={(e) => setEvent(e.target.value as BlockPrimaryEvent)}
        >
          {BLOCK_PRIMARY_EVENTS.map((e) => (
            <option key={e} value={e}>
              {e}
            </option>
          ))}
        </select>
      </label>

      {error && (
        <p className="text-xs text-rose-700 dark:text-rose-300">{error}</p>
      )}

      <div className="flex gap-2">
        <Button
          className="flex-1"
          onClick={submit}
          disabled={saving || !name.trim() || !start || !end}
        >
          {saving ? "Saving…" : submitLabel}
        </Button>
        <Button variant="outline" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function BlockRow({ block }: { block: TrainingBlock }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);

  const updateMut = useMutation({
    mutationFn: (body: BlockUpdateBody) =>
      apiPut<{ ok: boolean }>(
        `/api/training/blocks/${encodeURIComponent(block.id)}`,
        body as unknown as Record<string, unknown>,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["training", "blocks"] });
      qc.invalidateQueries({ queryKey: ["training", "weeks"] });
      qc.invalidateQueries({ queryKey: ["training", "cycle-stats"] });
      setEditing(false);
    },
  });

  const deleteMut = useMutation({
    mutationFn: () =>
      apiDelete<{ ok: boolean }>(
        `/api/training/blocks/${encodeURIComponent(block.id)}`,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["training", "blocks"] });
      qc.invalidateQueries({ queryKey: ["training", "weeks"] });
      qc.invalidateQueries({ queryKey: ["training", "cycle-stats"] });
      setConfirmDel(false);
    },
  });

  if (editing) {
    return (
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-semibold">Edit block</h4>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="text-muted-foreground hover:text-foreground"
              aria-label="Close"
            >
              <X className="size-4" />
            </button>
          </div>
          <BlockForm
            initial={block}
            saving={updateMut.isPending}
            error={
              updateMut.isError
                ? (updateMut.error as Error).message
                : undefined
            }
            submitLabel="Save"
            onSubmit={(body) => updateMut.mutate(body)}
            onCancel={() => setEditing(false)}
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent className="flex items-start justify-between gap-3 p-4">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold">{block.name}</p>
          <p className="text-xs text-muted-foreground">
            {block.start_date} → {block.end_date}
            {block.primary_event ? ` · ${block.primary_event}` : ""}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="rounded-md border border-border bg-background p-1.5 text-muted-foreground hover:bg-muted/40 hover:text-foreground"
            aria-label="Edit block"
          >
            <Pencil className="size-3.5" />
          </button>
          {confirmDel ? (
            <Button
              variant="destructive"
              size="sm"
              onClick={() => deleteMut.mutate()}
              disabled={deleteMut.isPending}
            >
              {deleteMut.isPending ? "…" : "Confirm?"}
            </Button>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmDel(true)}
              className="rounded-md border border-border bg-background p-1.5 text-muted-foreground hover:bg-rose-500/10 hover:text-rose-600"
              aria-label="Delete block"
            >
              <Trash2 className="size-3.5" />
            </button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function BlocksSection() {
  const qc = useQueryClient();
  const blocksQuery = useQuery({
    queryKey: ["training", "blocks"],
    queryFn: () => apiGet<BlocksResponse>("/api/training/blocks"),
  });
  const [creating, setCreating] = useState(false);

  const createMut = useMutation({
    mutationFn: (body: BlockCreateBody) =>
      apiPost<{ ok: boolean; id: string }>(
        "/api/training/blocks",
        body as unknown as Record<string, unknown>,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["training", "blocks"] });
      qc.invalidateQueries({ queryKey: ["training", "weeks"] });
      setCreating(false);
    },
  });

  const blocks = blocksQuery.data?.blocks ?? [];

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="font-heading text-lg font-semibold">Training blocks</h2>
        {!creating && (
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5"
            onClick={() => setCreating(true)}
          >
            <Plus className="size-3.5" />
            New
          </Button>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        A training block is one cycle (e.g. a marathon build). Runs auto-attach
        to whichever block covers their date.
      </p>

      {creating && (
        <Card>
          <CardContent className="space-y-3 p-4">
            <div className="flex items-center justify-between">
              <h4 className="text-sm font-semibold">New block</h4>
              <button
                type="button"
                onClick={() => setCreating(false)}
                className="text-muted-foreground hover:text-foreground"
                aria-label="Close"
              >
                <X className="size-4" />
              </button>
            </div>
            <BlockForm
              saving={createMut.isPending}
              error={
                createMut.isError
                  ? (createMut.error as Error).message
                  : undefined
              }
              submitLabel="Create"
              onSubmit={(body) => createMut.mutate(body)}
              onCancel={() => setCreating(false)}
            />
          </CardContent>
        </Card>
      )}

      {blocksQuery.isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : blocks.length === 0 ? (
        <p className="text-sm text-muted-foreground">No blocks yet.</p>
      ) : (
        <div className="space-y-2">
          {blocks.map((b) => (
            <BlockRow key={b.id} block={b} />
          ))}
        </div>
      )}
    </section>
  );
}
