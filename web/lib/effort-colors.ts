// One shared visual vocabulary for the user's six effort labels + Rest.
// Warm scale, light→dark with intensity, mirroring the coach's zone
// ordering. Chips and lap bars both draw from here so perceived effort
// always looks the same everywhere.
export const EFFORT_ORDER = [
  "Hold Back Easy",
  "Steady Effort",
  "Increasing Effort",
  "Marathon",
  "LT Effort",
  "VO2Max",
] as const;

export const EFFORT_COLORS: Record<string, string> = {
  "Hold Back Easy": "#F5C4B3",
  "Steady Effort": "#F0997B",
  "Increasing Effort": "#D85A30",
  Marathon: "#993C1D",
  "LT Effort": "#712B13",
  VO2Max: "#4A1B0C",
};

export const EFFORT_SHORT: Record<string, string> = {
  "Hold Back Easy": "Easy",
  "Steady Effort": "Steady",
  "Increasing Effort": "Incr.",
  Marathon: "M",
  "LT Effort": "LT",
  VO2Max: "VO2",
};

// Legacy free-form labels fall back to a neutral tone instead of
// crashing the color lookup.
export const EFFORT_FALLBACK = "#B4B2A9";

export function effortColor(category?: string | null): string {
  if (!category) return EFFORT_FALLBACK;
  return EFFORT_COLORS[category] ?? EFFORT_FALLBACK;
}
