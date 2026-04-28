export interface HealthDay {
  date: string;
  sleep_score: number | null;
  sleep_hours: number | null;
  rhr: number | null;
  hrv: number | null;
  stress: number | null;
  run_miles: number | null;
  run_mins: number | null;
}

export interface HealthTodayResponse {
  today: HealthDay;
  hrv_status: string;
}

export interface HealthTimelineResponse {
  days: number;
  timeline: HealthDay[];
}

export type Tone = "good" | "bad" | "flat" | "neutral";
export type Direction = "higher_better" | "lower_better" | "neutral";

export interface BaselineSummary {
  window: string;
  days: number;
  value: number | null;
  delta_pct: number | null;
  tone: Tone;
}

// Discriminated union of metric-specific extras. Each `type` introduces its
// own set of keys; consumers narrow by `type` and ignore anything they don't
// recognize. Future entries (e.g. training-readiness target zones) slot in
// here without affecting existing call sites.
export type MetricContext = HrvBandContext;

export interface HrvBandContext {
  type: "hrv_band";
  low_upper: number | null;
  balanced_low: number | null;
  balanced_upper: number | null;
  status: string | null;
}

export interface MetricSnapshot {
  key: string;
  label: string;
  value: number | null;
  unit: string | null;
  direction: Direction;
  // Map keyed by window-name. v1 only fills "recent"; future may add
  // "season_last_year", "trailing_3mo", etc. Keys we don't recognize on the
  // client are ignored, so adding new windows is non-breaking.
  baselines: Record<string, BaselineSummary>;
  context?: MetricContext;
}

export interface HealthSnapshot {
  date: string;
  baseline_window_days: number;
  metrics: MetricSnapshot[];
  behavior: {
    run_miles: number | null;
    run_mins: number | null;
  };
}

export interface SyncStatus {
  last_sync: string | null;
  last_attempt: string | null;
  outcome: "ok" | "token_expired" | "error" | null;
}

export interface SyncResult {
  ok: boolean;
  reason: "token_expired" | "error" | null;
  returncode?: number;
  stderr?: string;
}

export interface RefreshTokenResult {
  ok: boolean;
  returncode: number;
  stdout?: string;
  stderr?: string;
}

export interface SleepDetail {
  date: string;
  deep_min: number;
  rem_min: number;
  light_min: number;
  awake_min: number;
  total_min: number;
  avg_respiration: number | null;
  sleep_stress: number | null;
  sleep_start: string | null;
  sleep_end: string | null;
  body_battery_change: number | null;
  avg_hr: number | null;
  awake_count: number | null;
  avg_7d: {
    deep_min: number | null;
    rem_min: number | null;
    light_min: number | null;
    awake_min: number | null;
    total_min: number | null;
    avg_respiration: number | null;
    sleep_stress: number | null;
    body_battery_change: number | null;
    avg_hr: number | null;
    awake_count: number | null;
  };
}
