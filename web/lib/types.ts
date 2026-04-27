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
