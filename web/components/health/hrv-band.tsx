import type { HrvBandContext } from "@/lib/types";

// Garmin gives us a per-user calibrated HRV band: < lowUpper = low,
// [balancedLow, balancedUpper] = balanced, > balancedUpper = high.
// We render that as a horizontal mini bar with the balanced zone tinted and
// today's value as a tick. The visual range pads beyond the band so the tick
// doesn't sit on the edge when the value is near a boundary.

interface Props {
  value: number | null;
  context: HrvBandContext;
}

export function HrvBand({ value, context }: Props) {
  const { low_upper, balanced_low, balanced_upper, status } = context;
  if (
    value == null ||
    low_upper == null ||
    balanced_low == null ||
    balanced_upper == null
  ) {
    return null;
  }

  const padding = 6;
  const min = Math.min(value, low_upper) - padding;
  const max = Math.max(value, balanced_upper) + padding;
  const range = max - min || 1;
  const pct = (n: number) => ((n - min) / range) * 100;

  const balancedStart = pct(balanced_low);
  const balancedEnd = pct(balanced_upper);
  const valuePos = Math.max(2, Math.min(98, pct(value)));

  return (
    <div className="space-y-1">
      <div
        className="relative h-1.5 w-full rounded-full bg-muted"
        role="img"
        aria-label={`HRV band: balanced ${balanced_low}–${balanced_upper}, today ${value}`}
      >
        <div
          className="absolute inset-y-0 rounded-full bg-emerald-500/40"
          style={{ left: `${balancedStart}%`, right: `${100 - balancedEnd}%` }}
        />
        <div
          className="absolute top-1/2 h-3 w-0.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-foreground"
          style={{ left: `${valuePos}%` }}
        />
      </div>
      <div className="flex items-center justify-between text-[10px] tabular-nums text-muted-foreground">
        <span>balanced {balanced_low}–{balanced_upper}</span>
        {status && (
          <span className="font-medium uppercase tracking-wider">
            {status.toLowerCase()}
          </span>
        )}
      </div>
    </div>
  );
}
