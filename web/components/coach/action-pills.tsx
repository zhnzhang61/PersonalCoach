import {
  ClipboardList,
  HeartPulse,
  Brain,
} from "lucide-react";
import type { CoachActionName } from "@/lib/types";

interface Props {
  onAction: (name: CoachActionName) => void;
  disabled?: boolean;
}

// Three of the four utility actions live here as pills above the input.
// `review_workout` is omitted — it's launched from a specific run's
// detail page (where the activity_id is known), not the Coach tab.
// `summarize_and_archive` is the [End & Save] button rendered
// separately in the page header.
const PILLS: Array<{ name: CoachActionName; label: string; icon: React.ComponentType<{ className?: string }> }> = [
  { name: "make_plan", label: "Make Plan", icon: ClipboardList },
  { name: "review_health", label: "Review Health", icon: HeartPulse },
  { name: "follow_up_memory", label: "Memory", icon: Brain },
];

export function ActionPills({ onAction, disabled = false }: Props) {
  return (
    <div className="-mx-1 flex gap-1.5 overflow-x-auto px-1 pb-1 [&::-webkit-scrollbar]:hidden [scrollbar-width:none]">
      {PILLS.map((p) => {
        const Icon = p.icon;
        return (
          <button
            key={p.name}
            type="button"
            onClick={() => onAction(p.name)}
            disabled={disabled}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border bg-background px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted/40 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Icon className="size-3.5" aria-hidden />
            {p.label}
          </button>
        );
      })}
    </div>
  );
}
