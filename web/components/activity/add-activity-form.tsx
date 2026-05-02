"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { apiPost } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  ManualActivityForm,
  type ManualActivityFormValues,
} from "@/components/activity/manual-activity-form";
import type { ManualActivity } from "@/lib/types";

export function AddActivityForm() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);

  const mutation = useMutation({
    mutationFn: (payload: ManualActivityFormValues) =>
      apiPost<{ ok: boolean; activity: ManualActivity }>(
        "/api/manual-activities",
        payload as unknown as Record<string, unknown>,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["manual-activities"] });
      setOpen(false);
    },
  });

  if (!open) {
    return (
      <Button
        variant="outline"
        className="w-full justify-center gap-2"
        onClick={() => setOpen(true)}
      >
        <Plus className="size-4" aria-hidden />
        Add activity (swim / gym / manual run)
      </Button>
    );
  }

  return (
    <Card>
      <CardContent className="p-4">
        <ManualActivityForm
          title="New activity"
          pending={mutation.isPending}
          error={mutation.isError ? (mutation.error as Error).message : undefined}
          onSubmit={(v) => mutation.mutate(v)}
          onCancel={() => setOpen(false)}
        />
      </CardContent>
    </Card>
  );
}
