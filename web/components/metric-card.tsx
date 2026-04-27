import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export interface MetricCardProps {
  label: string;
  value: string;
  unit?: string;
  hint?: string;
  badge?: { text: string; tone?: "neutral" | "good" | "warn" | "bad" };
  loading?: boolean;
  className?: string;
}

const toneStyles: Record<string, string> = {
  neutral: "",
  good: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/20",
  warn: "bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/20",
  bad: "bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/20",
};

export function MetricCard({
  label,
  value,
  unit,
  hint,
  badge,
  loading,
  className,
}: MetricCardProps) {
  return (
    <Card className={cn("h-full", className)}>
      <CardContent className="flex h-full flex-col gap-2 p-5">
        <div className="flex items-start justify-between gap-2">
          <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
            {label}
          </span>
          {badge && !loading && (
            <Badge
              variant="outline"
              className={cn(
                "shrink-0 text-[10px] font-medium",
                toneStyles[badge.tone ?? "neutral"],
              )}
            >
              {badge.text}
            </Badge>
          )}
        </div>
        {loading ? (
          <Skeleton className="h-9 w-24" />
        ) : (
          <div className="flex items-baseline gap-1">
            <span className="text-3xl font-semibold tabular-nums leading-none">
              {value}
            </span>
            {unit && (
              <span className="text-sm font-medium text-muted-foreground">
                {unit}
              </span>
            )}
          </div>
        )}
        {hint &&
          (loading ? (
            <Skeleton className="mt-1 h-3 w-32" />
          ) : (
            <span className="text-xs text-muted-foreground">{hint}</span>
          ))}
      </CardContent>
    </Card>
  );
}
