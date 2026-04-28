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
