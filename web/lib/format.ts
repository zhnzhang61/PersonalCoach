import { format, parseISO } from "date-fns";

export function fmtNum(
  n: number | null | undefined,
  digits = 0,
): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

export function fmtDate(s: string, pattern = "MMM d"): string {
  return format(parseISO(s), pattern);
}
