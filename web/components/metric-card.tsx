import Link from "next/link";
import { ChevronRight } from "lucide-react";
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
  href?: string;
  className?: string;
}

const toneStyles: Record<string, string> = {
  neutral: "border-border/60 bg-muted/40 text-foreground/70",
  good: "border-emerald-600/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  warn: "border-amber-600/30 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  bad: "border-rose-600/30 bg-rose-500/10 text-rose-700 dark:text-rose-300",
};

export function MetricCard({
  label,
  value,
  unit,
  hint,
  badge,
  loading,
  href,
  className,
}: MetricCardProps) {
  const inner = (
    <Card
      className={cn(
        "h-full transition-colors",
        href && "hover:border-warm-accent/40 hover:bg-warm-accent-soft/30",
        className,
      )}
    >
      <CardContent className="flex h-full flex-col gap-3 p-5">
        <div className="flex items-start justify-between gap-2">
          <span className="eyebrow">{label}</span>
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
          {href && !badge && (
            <ChevronRight className="size-4 text-muted-foreground" aria-hidden />
          )}
        </div>
        {loading ? (
          <Skeleton className="h-10 w-24" />
        ) : (
          <div className="flex items-baseline gap-1.5">
            <span className="font-heading text-4xl font-semibold tabular-nums leading-none sm:text-[2.75rem]">
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
            <Skeleton className="mt-auto h-3 w-32" />
          ) : (
            <span className="mt-auto text-xs text-muted-foreground">{hint}</span>
          ))}
      </CardContent>
    </Card>
  );

  if (href) {
    return (
      <Link
        href={href}
        className={cn("block focus-visible:outline-none", className)}
      >
        {inner}
      </Link>
    );
  }
  return inner;
}
