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

// ==========================================
// Training blocks / weeks / cycle stats
// ==========================================
export interface TrainingBlock {
  id: string;
  name: string;
  start_date: string;
  end_date: string;
  primary_event?: string;
}

export interface BlocksResponse {
  blocks: TrainingBlock[];
  active_block_id: string | null;
}

// Primary event options — kept in lockstep with dashboard's setup form.
export const BLOCK_PRIMARY_EVENTS = [
  "running",
  "cycling",
  "triathlon",
  "strength",
  "other",
] as const;
export type BlockPrimaryEvent = (typeof BLOCK_PRIMARY_EVENTS)[number];

export interface BlockCreateBody {
  name: string;
  start_date: string;
  end_date: string;
  primary_event: BlockPrimaryEvent;
}

export interface BlockUpdateBody {
  name?: string;
  start_date?: string;
  end_date?: string;
  primary_event?: BlockPrimaryEvent;
}

export interface TrainingWeek {
  week_num: number;
  start: string;
  end: string;
  label: string;
}

export interface WeeksResponse {
  block_id: string;
  weeks: TrainingWeek[];
}

export interface CategoryBreakdownRow {
  effort: string;
  miles: number;
  pct_of_total: number;
  avg_pace: string;
  avg_hr: number | null;
  elevation_ft: number | null;
}

export interface WeeklyMileBar {
  week_num: number;
  label: string;
  miles: number;
}

export interface CycleSummary {
  total_runs: number;
  total_miles: number;
  total_hours: number;
  avg_pace: string;
  avg_hr: number;
  elevation_ft: number;
  calories: number;
  longest_run: number;
  avg_weekly_miles: number;
  category_breakdown: CategoryBreakdownRow[];
}

export interface WeekSummary {
  week_num: number;
  runs: number;
  miles: number;
  hours: number;
  avg_pace: string;
  avg_hr: number;
  elevation_ft: number;
  vs_avg: number;
}

export interface CycleStatsResponse {
  block_id: string;
  block_name: string;
  cycle: CycleSummary;
  week: WeekSummary;
  weekly_miles: WeeklyMileBar[];
}

// ==========================================
// Historical monthly stats (Training tab → Historical chart)
// ==========================================
export interface MonthlyActivityStats {
  month: string; // YYYY-MM
  count: number;
  miles: number;
  hours: number;
  elevation_ft: number;
  avg_pace_dec: number | null; // min/mi as decimal; null when miles == 0
  avg_pace: string | null; // pre-formatted "M:SS" for prompt/tooltip use
  avg_hr: number | null;
}

export interface MonthlyStatsResponse {
  activity_type: string;
  months: MonthlyActivityStats[];
}

// ==========================================
// Run activities (Garmin). Extra Garmin keys may be present and are ignored.
// ==========================================
export interface RunCategoryStat {
  category: string;
  distance_mi: number;
  pace: string;
  avg_hr: number;
}

export interface RunManualMeta {
  name?: string;
  notes?: string;
  week_num?: number;
  category_stats?: RunCategoryStat[];
  lap_categories?: string[];
}

export interface RunActivity {
  activityId: number;
  activityName?: string;
  activityType?: { typeKey?: string };
  startTimeLocal?: string;
  distance?: number;
  movingDuration?: number;
  duration?: number;
  averageHR?: number;
  elevationGain?: number;
  manual_meta?: RunManualMeta;
}

export interface RunsResponse {
  start: string;
  end: string;
  runs: RunActivity[];
}

export interface RunDetailResponse {
  run: RunActivity;
  laps: unknown[];
  chat_history: unknown;
}

// Effort categories — must match data_processor.calculate_category_stats /
// dashboard's lap-categorize dropdown. Keep in lockstep.
export const EFFORT_CATEGORIES = [
  "Hold Back Easy",
  "Steady Effort",
  "Increasing Effort",
  "Marathon",
  "LT Effort",
  "VO2Max",
  "Sprint",
  "Rest",
] as const;
export type EffortCategory = (typeof EFFORT_CATEGORIES)[number];

export interface Lap {
  distance: number; // meters
  duration: number; // seconds
  averageHR?: number;
  elevationGain?: number; // meters
  category: string; // EffortCategory or legacy free-form
}

export interface LapsResponse {
  activity_id: number;
  laps: Lap[];
  meta: RunManualMeta;
}

export interface LapsUpdateBody {
  week_num: number;
  run_name: string;
  categories: string[];
  notes: string;
}

// Per-second telemetry from Garmin's activity-detail metrics. Older runs may
// not have every field — `null` means the metric wasn't reported that second.
export interface TelemetryRow {
  Lap: number;
  Second: number;
  Distance?: number | null; // cumulative miles
  HeartRate?: number | null;
  Speed_mps?: number | null;
  Pace?: number | null; // min/mi
  Cadence?: number | null;
  Elevation?: number | null;
  StrideLength?: number | null; // cm
  RespirationRate?: number | null; // breaths/min
  VerticalOscillation?: number | null; // cm
  GroundContactTime?: number | null; // ms
  GroundContactBalanceLeft?: number | null; // % left foot, e.g. 49.3 → 49.3 / 50.7
  Power?: number | null; // watts
  AirTemperature?: number | null; // celsius — Garmin's wrist sensor (unreliable)
}

export interface MetricSummary {
  avg: number;
  min: number;
  max: number;
}

// Server computes per-metric stats once so the client doesn't have to
// re-derive them (and re-implement filtering rules like the pace clip).
export type TelemetrySummaryKey =
  | "HeartRate"
  | "Pace"
  | "StrideLength"
  | "Cadence"
  | "RespirationRate"
  | "GroundContactBalanceLeft"
  | "Elevation";

export interface TelemetryResponse {
  raw: TelemetryRow[];
  ai: Record<string, unknown>[];
  summary: Partial<Record<TelemetrySummaryKey, MetricSummary | null>>;
  pace_clip: [number, number]; // min/mi bounds applied when computing pace stats
}

export type LatLng = [number, number]; // [lat, lon]

export interface RouteResponse {
  activity_id: number;
  polyline: LatLng[];
  start: LatLng;
  end: LatLng;
  bounds: {
    min_lat: number | null;
    max_lat: number | null;
    min_lon: number | null;
    max_lon: number | null;
  };
}

export interface WeatherSnapshot {
  activity_id: number;
  lat: number;
  lon: number;
  hour_local: string; // "YYYY-MM-DDTHH:00"
  temperature_c: number | null;
  temperature_f: number | null;
  apparent_temperature_c: number | null;
  apparent_temperature_f: number | null;
  humidity_pct: number | null;
  dew_point_c: number | null;
  dew_point_f: number | null;
  source: "open-meteo";
  fetched_at: string;
}

// ==========================================
// Manual activities (non-Garmin: swim/gym/free-form runs)
// ==========================================
export type ManualActivityType = "run" | "swim" | "gym" | "other";

export interface ManualActivity {
  id: string;
  date: string;
  type: ManualActivityType | string;
  desc: string;
  duration_min?: number;
  distance_mi?: number;
  // "HH:MM" — optional. When present, the activity gets a real timed
  // window on the calendar; when absent it renders all-day.
  start_time?: string | null;
}

export interface ManualActivitiesResponse {
  start: string;
  end: string;
  activities: ManualActivity[];
}

// ==========================================
// Daily check-ins (PR P3 — perceived layer)
// ==========================================
//
// One row per calendar date. The 4 scale fields are 0–5 ordinals
// (0 = "didn't capture" or "none" for soreness); the agent
// interprets `null` as "user didn't answer this slider". Notes are
// optional free text — preserved verbatim for both the agent and
// the user's later self-review.
export interface DailyCheckin {
  date: string; // YYYY-MM-DD
  sleep_quality?: number | null;
  soreness?: number | null;
  mood?: number | null;
  motivation?: number | null;
  notes?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface CheckinsResponse {
  days: number;
  start: string;
  end: string;
  checkins: DailyCheckin[];
}

// ==========================================
// External context events (PR P5 — external context §4)
// ==========================================
//
// User-logged events that contextualize sensor data: travel days
// (jet lag), illness ranges, life-stress windows. Saved as CME
// episodes with event_type ∈ {travel, illness, life_stress}; the
// context dict carries start_date / end_date / description so the
// list endpoint can range-filter without parsing prose.
export type ExternalEventType = "travel" | "illness" | "life_stress";

export interface ExternalEvent {
  episode_id: string;
  event_type: ExternalEventType;
  start_date: string; // YYYY-MM-DD inclusive
  end_date: string;
  context: {
    start_date?: string;
    end_date?: string;
    description?: string;
    [key: string]: unknown;
  };
  lesson_learned?: string | null;
  timestamp?: string;
  related_topic_ids?: string[];
}

export interface ExternalEventsResponse {
  start: string;
  end: string;
  events: ExternalEvent[];
}

// ==========================================
// Planned workouts (PR P4a — intent layer §3, P4b — UI)
// ==========================================
//
// `planned_workouts.json` rows. `cal_event_id` is present when the
// row was dual-written to Google Cal (which happens automatically
// during create when the user is connected). Edits via our PUT
// endpoint sync back to Cal when this id is non-null; deletes via
// DELETE remove the Cal event too.
export type PlannedWorkoutType =
  | "easy"
  | "tempo"
  | "interval"
  | "long"
  | "run"
  | "swim"
  | "gym"
  | "other";

export interface PlannedWorkout {
  id: string;
  date: string; // YYYY-MM-DD
  type: PlannedWorkoutType;
  target_pace_min_mi?: number | null;
  target_hr?: number | null;
  distance_mi?: number | null;
  duration_min?: number | null;
  notes?: string | null;
  cal_event_id?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface PlannedWorkoutsResponse {
  start: string;
  end: string;
  planned_workouts: PlannedWorkout[];
}

// Plan-vs-actual deviation for a single run (PR P4b). Returned by
// `GET /api/runs/{id}/plan-deviation`. `deltas` follows the
// "actual - planned" convention; only keys whose plan side was
// populated are emitted.
export interface PlanDeviation {
  matched: boolean;
  planned: PlannedWorkout | null;
  actual: {
    date: string;
    distance_mi: number;
    duration_min: number;
    pace_min_mi: number | null;
    avg_hr: number | null;
  } | null;
  deltas: {
    pace_min_mi?: number;
    hr?: number;
    distance_mi?: number;
    duration_min?: number;
  } | null;
}

// ==========================================
// Unified calendar events (Training tab → Plan calendar)
// ==========================================
// After server-side normalisation, Google Calendar events + ManualActivity
// rows + Garmin runs all flatten into the same shape. The `source`
// discriminator drives both UI styling and AI reasoning ("this is a real
// commitment" vs "this is a planned workout").
export type CalendarEventSource =
  | "google"
  | "google_error"
  | "manual"
  | "garmin_run"
  // Google events whose description contains the
  // PLANNED_WORKOUT_MARKER — re-classified server-side so the UI can
  // dye AI-authored workouts distinctly from generic life events.
  | "planned_workout";

export interface CalendarEvent {
  source: CalendarEventSource;
  id: string;
  title: string;
  start: string; // ISO datetime, or YYYY-MM-DD for all-day
  end: string;
  all_day: boolean;
  location?: string | null;
  description?: string | null;
  calendar_id?: string;
  activity_id?: number;
  manual_activity?: ManualActivity;
}

export interface CalendarEventsResponse {
  start: string;
  end: string;
  google_connected: boolean;
  events: CalendarEvent[];
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

// ==========================================
// Coach chat (session-based; see docs/coach_chat_design.md)
// ==========================================

export type CoachActionName =
  | "review_workout"
  | "make_plan"
  | "review_health"
  | "follow_up_memory"
  | "summarize_and_archive";

export interface CoachMessage {
  role: "human" | "ai" | "system" | "tool";
  content: string;
  ts?: string;
}

export interface CoachSession {
  thread_id: string;
  started_at: string | null;
  closed_at: string | null;
  summary: string | null;
  topics_added: number;
  episodes_added: number;
  message_count: number;
}

export interface CoachSessionsResponse {
  sessions: CoachSession[];
  limit: number;
  before: string | null;
}

export interface CoachChatResponse {
  thread_id: string;
  answer: string;
}

export interface CoachActionResponse {
  thread_id: string;
  answer?: string;
  // archive-only fields:
  summary?: string | null;
  topics_added?: number;
  episodes_added?: number;
  closed_at?: string | null;
  consolidation?: unknown;
  // error path:
  error?: string;
  traceback?: string;
}

export interface CoachHistoryResponse {
  thread_id: string;
  messages: CoachMessage[];
}
