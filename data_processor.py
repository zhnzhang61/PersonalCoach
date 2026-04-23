import os
import json
import csv
import datetime
from datetime import timedelta
import pandas as pd
import numpy as np

class DataProcessor:
    def __init__(self, data_dir='data'):
        self.data_dir = data_dir
        
        # --- PATH DEFINITIONS (Upgraded for Agentic Memory Architecture) ---
        self.paths = {
            # 1. Raw Data Paths (Garmin Sync)
            'activities': os.path.join(data_dir, 'get_activities'),
            'splits': os.path.join(data_dir, 'get_activity_splits'),
            'hr_zones': os.path.join(data_dir, 'get_activity_hr_in_timezones'),
            'sleep': os.path.join(data_dir, 'get_sleep_data'),
            'rhr': os.path.join(data_dir, 'get_rhr_day'),
            'hrv': os.path.join(data_dir, 'get_hrv_data'),
            'stress': os.path.join(data_dir, 'get_stress_data'),
            'details': os.path.join(data_dir, 'get_activity_details'),
            'stats_body': os.path.join(data_dir, 'get_stats_and_body'),
            'training_readiness': os.path.join(data_dir, 'get_training_readiness'),
            'training_status': os.path.join(data_dir, 'get_training_status'),
            'respiration': os.path.join(data_dir, 'get_respiration_data'),
            'fitness_age': os.path.join(data_dir, 'get_fitnessage_data'),
            'intensity_min': os.path.join(data_dir, 'get_intensity_minutes_data'),
            
            # 2. Derived & Manual Paths
            'manual': os.path.join(data_dir, 'manual_inputs'),
            'blocks': os.path.join(data_dir, 'blocks', 'training_blocks.json'),
            'aux': os.path.join(data_dir, 'blocks', 'auxiliary_log.json'),
            'ledger': os.path.join(data_dir, 'derived', 'daily_health_metrics.csv'),
            
            # 3. AI Memory Paths (NEW)
            'semantic_memory': os.path.join(data_dir, 'memory', 'user_profile.json'),
            'episodic_memory': os.path.join(data_dir, 'memory', 'episodic_logs.json')
        }
        self._ensure_infrastructure()

    def _ensure_infrastructure(self):
        """Creates required directories and initializes core memory files if missing."""
        for path in self.paths.values():
            if path.endswith('.json') or path.endswith('.csv'):
                os.makedirs(os.path.dirname(path), exist_ok=True)
            else:
                os.makedirs(path, exist_ok=True)
                
        # Initialize Blocks (empty list — users create their own via the UI)
        if not os.path.exists(self.paths['blocks']):
            with open(self.paths['blocks'], 'w') as f:
                json.dump([], f, indent=4)
            
        # Initialize Aux
        if not os.path.exists(self.paths['aux']):
            with open(self.paths['aux'], 'w') as f: json.dump([], f)
            
        # Initialize Semantic Memory (User Profile)
        if not os.path.exists(self.paths['semantic_memory']):
            raw_profile_path = os.path.join(self.data_dir, 'get_user_profile', 'latest.json')
            
            # 直接读取你本地真实的 Garmin profile
            garmin_data = {}
            if os.path.exists(raw_profile_path):
                try:
                    with open(raw_profile_path, 'r') as f:
                        garmin_data = json.load(f)
                except Exception as e:
                    print(f"⚠️ Could not load Garmin profile: {e}")
            
            # 合并到我们的 Semantic Memory 中
            default_profile = {
                "garmin_profile": garmin_data, 
                "medical_notes": ["No known injuries."], 
                "preferences": ["Prefers pace in min/mi"]
            }
            
            # 如果本地还没抓到佳明档案，给个默认结构兜底
            if not garmin_data:
                default_profile["user_basics"] = {"name": "Athlete", "age": None, "weight_kg": None}
                default_profile["physiological_baseline"] = {"max_hr": 190, "resting_hr": 50, "lt_hr": 165}
                
            with open(self.paths['semantic_memory'], 'w') as f: 
                json.dump(default_profile, f, indent=4)

        # Initialize Episodic Memory (Historical AI Summaries)
        if not os.path.exists(self.paths['episodic_memory']):
            with open(self.paths['episodic_memory'], 'w') as f: json.dump([], f)

    def load_json_safe(self, folder_or_path, filename=None):
        """Safely loads JSON from either a full path or folder+filename."""
        try:
            path = os.path.join(folder_or_path, filename) if filename else folder_or_path
            if not os.path.exists(path): return {}
            with open(path, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    # ==========================================
    # 🧠 TIER 1: SEMANTIC MEMORY (User Profile)
    # ==========================================
    def get_semantic_memory(self):
        """Returns the absolute truths about the user to be injected into the System Prompt."""
        return self.load_json_safe(self.paths['semantic_memory'])

    def update_semantic_memory(self, category, key, value):
        """Allows the AI Tool to permanently update the user's profile."""
        profile = self.get_semantic_memory()
        if category not in profile:
            profile[category] = {}
        
        if isinstance(profile[category], dict):
            profile[category][key] = value
        elif isinstance(profile[category], list):
            if value not in profile[category]:
                profile[category].append(value)
                
        with open(self.paths['semantic_memory'], 'w') as f:
            json.dump(profile, f, indent=4)

    # ==========================================
    # 🧠 TIER 2: EPISODIC MEMORY (Historical Summaries)
    # ==========================================
    def save_episodic_memory(self, activity_id, date, summary_text, tags=None):
        """Saves a dense LLM-generated summary of a workout for future RAG retrieval."""
        memories = self.load_json_safe(self.paths['episodic_memory']) or []
        
        # Remove existing if overwriting
        memories = [m for m in memories if m['activity_id'] != str(activity_id)]
        
        memories.append({
            "activity_id": str(activity_id),
            "date": date,
            "tags": tags or [],
            "summary": summary_text
        })
        
        # Sort chronologically
        memories.sort(key=lambda x: x['date'], reverse=True)
        with open(self.paths['episodic_memory'], 'w') as f:
            json.dump(memories, f, indent=4)

    def search_episodic_memories(self, limit=5, require_tags=None):
        """Allows the AI to fetch similar historical workouts based on tags (e.g., 'Long Run')."""
        memories = self.load_json_safe(self.paths['episodic_memory']) or []
        if require_tags:
            require_set = set(require_tags)
            memories = [m for m in memories if require_set.intersection(set(m['tags']))]
        return memories[:limit]

    def append_chat_to_episodic_memory(self, activity_id, chat_summary):
        """将深度的对话总结追加到当次训练的情景记忆中"""
        memories = self.load_json_safe(self.paths['episodic_memory']) or []
        updated = False
        
        for mem in memories:
            if mem['activity_id'] == str(activity_id):
                # 将对话总结作为 "coach_advice" 字段存入永久档案
                mem['coach_advice'] = chat_summary
                updated = True
                break
                
        if updated:
            with open(self.paths['episodic_memory'], 'w') as f:
                json.dump(memories, f, indent=4)

    # ==========================================
    # 🧬 HEALTH DATA AGGREGATION (Daily Readiness)
    # ==========================================
    def compile_health_ledger(self, days_back=120):
        records = []
        today = datetime.date.today()
        activity_map = {}
        
        if os.path.exists(self.paths['activities']):
            for f in os.listdir(self.paths['activities']):
                if f.endswith('_summary.json'):
                    act = self.load_json_safe(self.paths['activities'], f)
                    if not act: continue
                    raw_date = act.get('startTimeLocal')
                    if not raw_date: continue
                    d_str = raw_date[:10]
                    if d_str not in activity_map: activity_map[d_str] = {'dist': 0, 'time': 0}
                    activity_map[d_str]['dist'] += act.get('distance', 0)
                    activity_map[d_str]['time'] += act.get('duration', 0)

        for i in range(days_back):
            date_str = (today - timedelta(days=i)).isoformat()
            sleep = self.load_json_safe(self.paths['sleep'], f"{date_str}.json")
            rhr = self.load_json_safe(self.paths['rhr'], f"{date_str}.json")
            hrv = self.load_json_safe(self.paths['hrv'], f"{date_str}.json")
            stress = self.load_json_safe(self.paths['stress'], f"{date_str}.json")
            
            sleep_score = sleep.get('dailySleepDTO', {}).get('sleepScores', {}).get('overall', {}).get('value')
            sleep_sec = sleep.get('dailySleepDTO', {}).get('sleepTimeSeconds', 0)
            
            rhr_metrics = rhr.get('allMetrics', {}).get('metricsMap', {}).get('WELLNESS_RESTING_HEART_RATE', [])
            rhr_val = rhr_metrics[0].get('value') if rhr_metrics else None
            
            hrv_val = hrv.get('hrvSummary', {}).get('weeklyAvg')
            if hrv.get('hrvData'): hrv_val = hrv.get('hrvData', {}).get('lastNightAvg')

            stress_val = stress.get('avgStressLevel')
            daily_run = activity_map.get(date_str, {'dist': 0, 'time': 0})

            records.append({
                'date': date_str,
                'sleep_score': sleep_score,
                'sleep_hours': round(sleep_sec / 3600, 2) if sleep_sec else None,
                'rhr': rhr_val,
                'hrv': hrv_val,
                'stress': stress_val,
                'run_miles': round(daily_run['dist'] / 1609.34, 2),
                'run_mins': round(daily_run['time'] / 60, 1)
            })

        records.sort(key=lambda x: x['date'])
        if records:
            keys = records[0].keys()
            with open(self.paths['ledger'], 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(records)
        return records

    def get_health_stats(self):
        if not os.path.exists(self.paths['ledger']): return self.compile_health_ledger()
        data = []
        try:
            with open(self.paths['ledger'], 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    for k, v in row.items():
                        if k != 'date': row[k] = float(v) if v and v != 'None' else None
                    data.append(row)
        except Exception:
            return self.compile_health_ledger()
        return data

    def get_daily_readiness(self, target_date_str):
        """Pulls the health metrics for a specific date from the ledger."""
        ledger = self.get_health_stats()
        return next((row for row in ledger if row['date'] == target_date_str), None)

    # ==========================================
    # ⚡ AGENT CONTEXT BUILDER (Working Memory)
    # ==========================================
    def build_agent_working_memory(self, activity_id, block_id=None):
        """
        MASTER AGGREGATOR: Combines Profile + Readiness + Workout Data into ONE dense dict.
        Feed this directly into the LLM as JSON/YAML context.
        """
        # 1. Fetch Workout Meta
        meta_path = os.path.join(self.paths['manual'], f"run_{activity_id}_meta.json")
        if not os.path.exists(meta_path): return {"error": "Activity metadata not found"}
        with open(meta_path) as f: run_meta = json.load(f)
        
        # 2. Extract Date & HR Zones
        run_date = datetime.date.today().isoformat()
        for f in os.listdir(self.paths['activities']):
            if f.endswith('_summary.json') and str(activity_id) in f:
                act = self.load_json_safe(self.paths['activities'], f)
                run_date = act.get('startTimeLocal', '')[:10]
                break

        # 3. Fetch Daily Health Readiness for THAT specific day
        readiness = self.get_daily_readiness(run_date)

        # 4. Fetch Aux Activities (Last 7 days)
        date_obj = datetime.date.fromisoformat(run_date)
        start_7 = (date_obj - timedelta(days=7)).isoformat()
        aux_events = self.get_aux_in_range(start_7, run_date)

        # 5. Assemble The Ultimate Context Payload
        context = {
            "agent_directive": "Analyze this workout combining physiological baseline and daily readiness.",
            "date": run_date,
            "daily_readiness": readiness,
            "workout_summary": {
                "name": run_meta.get('name'),
                "notes": run_meta.get('notes'),
                "category_stats": run_meta.get('category_stats')
            },
            "recent_aux_activities": aux_events
        }
        
        # If block info is requested, append it
        if block_id:
            block = next((b for b in self.get_blocks() if b['id'] == block_id), {})
            context["training_block_goal"] = block.get('name')

        return context

    # ==========================================
    # 🏃 TELEMETRY & EXISTING METHODS
    # ==========================================
    
    def get_blocks(self):
        blocks = self.load_json_safe(self.paths['blocks'])
        return blocks if isinstance(blocks, list) else []

    def _save_blocks(self, blocks: list[dict]) -> None:
        """Sort by start_date descending (newest first) before persisting."""
        blocks.sort(key=lambda b: b.get('start_date', ''), reverse=True)
        with open(self.paths['blocks'], 'w') as f:
            json.dump(blocks, f, indent=4)

    def _next_block_id(self, blocks: list[dict]) -> str:
        """Smallest block_NNN id not already in use."""
        existing = {b.get('id') for b in blocks}
        n = 1
        while f"block_{n:03d}" in existing:
            n += 1
        return f"block_{n:03d}"

    def create_block(self, name: str, start_date: str, end_date: str,
                     primary_event: str = "running") -> str:
        """
        Append a new training block. Caller must pass ISO dates (YYYY-MM-DD)
        and ensure end_date ≥ start_date — validation at call site keeps
        UI errors close to where the user can fix them.
        """
        if not name or not name.strip():
            raise ValueError("Block name cannot be empty")
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        blocks = self.get_blocks()
        new_id = self._next_block_id(blocks)
        blocks.append({
            "id": new_id,
            "name": name.strip(),
            "start_date": start_date,
            "end_date": end_date,
            "primary_event": primary_event,
        })
        self._save_blocks(blocks)
        return new_id

    def update_block(self, block_id: str, **fields) -> bool:
        """
        Patch an existing block. Silently drops the deprecated
        baseline_snapshot field on write. Returns False if the id is unknown.
        """
        blocks = self.get_blocks()
        for b in blocks:
            if b.get('id') != block_id:
                continue
            if 'end_date' in fields and 'start_date' in fields:
                if fields['end_date'] < fields['start_date']:
                    raise ValueError("end_date must be on or after start_date")
            for k, v in fields.items():
                if k == 'id':
                    continue
                b[k] = v
            b.pop('baseline_snapshot', None)
            self._save_blocks(blocks)
            return True
        return False

    def delete_block(self, block_id: str) -> bool:
        """Remove a block by id. Run/episode files are untouched — they live
        separately and re-attach to whatever block covers their date range."""
        blocks = self.get_blocks()
        remaining = [b for b in blocks if b.get('id') != block_id]
        if len(remaining) == len(blocks):
            return False
        self._save_blocks(remaining)
        return True

    # --- RESTORED METHODS FOR UI ---
    def get_weeks_for_block(self, block_id):
        """
        Split a block into ISO-style weeks (Monday-start). Week 0 is the
        partial week from the block's start date up to the first Sunday;
        subsequent weeks are full 7-day windows Monday→Sunday. Returns
        [] if the block id is unknown.
        """
        blocks = self.get_blocks()
        block = next((b for b in blocks if b['id'] == block_id), None)
        if not block:
            return []
        start = datetime.date.fromisoformat(block['start_date'])
        end = datetime.date.fromisoformat(block['end_date'])
        weeks = []

        # Week 0 — from block start through the first Sunday (or block end,
        # whichever is earlier). Monday=0 … Sunday=6 in Python's weekday().
        days_until_sunday = 6 - start.weekday()
        w0_end = min(start + timedelta(days=days_until_sunday), end)
        weeks.append({
            "week_num": 0,
            "start": start.isoformat(),
            "end": w0_end.isoformat(),
            "label": f"Week 0 ({w0_end.strftime('%b %d')})",
        })
        curr = w0_end + timedelta(days=1)
        week_num = 1

        while curr <= end:
            w_end = min(curr + timedelta(days=6), end)
            weeks.append({
                "week_num": week_num,
                "start": curr.isoformat(),
                "end": w_end.isoformat(),
                "label": f"Week {week_num} ({w_end.strftime('%b %d')})",
            })
            curr += timedelta(days=7)
            week_num += 1
        return weeks

    def get_activities_in_range(self, start_str, end_str):
        found = []
        if not os.path.exists(self.paths['activities']): return []
        
        start_date = datetime.date.fromisoformat(start_str)
        end_date = datetime.date.fromisoformat(end_str)

        for f in os.listdir(self.paths['activities']):
            if not f.endswith('.json'): continue
            try:
                with open(os.path.join(self.paths['activities'], f)) as jf:
                    content = json.load(jf)
                    activity_list = content if isinstance(content, list) else [content]

                    for data in activity_list:
                        raw_date = data.get('startTimeLocal', '')
                        if not raw_date: continue
                        act_date = datetime.date.fromisoformat(raw_date[:10])

                        if start_date <= act_date <= end_date:
                            meta_path = os.path.join(self.paths['manual'], f"run_{data['activityId']}_meta.json")
                            meta = {}
                            if os.path.exists(meta_path):
                                with open(meta_path) as mf: meta = json.load(mf)
                            
                            found.append({**data, "manual_meta": meta})
            except Exception: continue
        return sorted(found, key=lambda x: x['startTimeLocal'], reverse=True)
    # -------------------------------

    def get_aux_in_range(self, start_str, end_str):
        current = self.load_json_safe(self.paths['aux'])
        if isinstance(current, dict): current = [] # Fallback
        return [x for x in current if start_str <= x['date'] <= end_str]

    def add_aux_activity(self, date_str, event_type, desc):
        with open(self.paths['aux'], 'r') as f: current = json.load(f)
        current.append({"id": f"aux_{int(datetime.datetime.now().timestamp())}", "date": date_str, "type": event_type, "desc": desc})
        with open(self.paths['aux'], 'w') as f: json.dump(current, f, indent=4)

    def calculate_category_stats(self, labeled_laps):
        groups = {}
        valid_cats = ["Hold Back Easy", "Steady Effort", "Increasing Effort", "Marathon", "LT Effort", "VO2Max", "Sprint", "Rest"]
        
        for lap in labeled_laps:
            cat = lap.get('category', 'Rest')
            if cat not in valid_cats: cat = "Rest"
            
            if cat not in groups:
                groups[cat] = {'total_dist': 0.0, 'total_time': 0.0, 'weighted_hr_sum': 0.0}
            
            dist = lap.get('distance', 0)
            dur = lap.get('duration', 0)
            hr = lap.get('averageHR', 0)
            
            groups[cat]['total_dist'] += dist
            groups[cat]['total_time'] += dur
            groups[cat]['weighted_hr_sum'] += (hr * dist)

        results = []
        for cat, stats in groups.items():
            t_dist = stats['total_dist']
            t_time = stats['total_time']
            
            pace_str = "N/A"
            if t_dist > 0 and t_time > 0:
                speed_mps = t_dist / t_time
                pace_decimal = (1609.34 / speed_mps) / 60
                pace_str = f"{int(pace_decimal)}:{int((pace_decimal % 1) * 60):02d}"

            avg_hr = int(stats['weighted_hr_sum'] / t_dist) if t_dist > 0 else 0
            
            results.append({
                "category": cat,
                "distance_mi": round(t_dist / 1609.34, 2),
                "pace": pace_str,
                "avg_hr": avg_hr
            })
        return results

    def save_run_metadata(self, activity_id, week_num, run_name, category_stats, notes="", lap_categories=None):
        meta = {
            "name": run_name,
            "week_num": week_num,
            "category_stats": category_stats,
            "updated_at": datetime.datetime.now().isoformat(),
            "notes": notes,
            "lap_categories": lap_categories if lap_categories else [] 
        }
            
        with open(os.path.join(self.paths['manual'], f"run_{activity_id}_meta.json"), 'w') as f:
            json.dump(meta, f, indent=4)

    def get_run_laps(self, activity_id):
        json_path = os.path.join(self.paths['splits'], f"{activity_id}.json")
        if not os.path.exists(json_path): return []
        try:
            with open(json_path, 'r') as f:
                data = json.load(f)
                laps = data.get('lapDTOs', [])
                if laps and laps[-1].get('duration', 0) < 10: laps.pop()
                return laps
        except Exception: return []

    def get_activity_telemetry(self, activity_id, laps=None, downsample_sec=10):
        """Unchanged downsampling logic for the UI/Charts."""
        file_path = os.path.join(self.paths['details'], f"{activity_id}.json")
        if not os.path.exists(file_path): return None, None

        with open(file_path, 'r') as f: raw_data = json.load(f)

        metrics_desc = raw_data.get('metricDescriptors', [])
        metric_map = { m['key']: m['metricsIndex'] for m in metrics_desc }

        def get_val(row_metrics, key):
            idx = metric_map.get(key)
            if idx is not None and idx < len(row_metrics): return row_metrics[idx]
            return None

        lap_boundaries = []
        if laps:
            cum_time = 0
            for i, lap in enumerate(laps):
                cum_time += lap.get('duration', 0)
                lap_boundaries.append((cum_time, i + 1))
        
        def get_lap(sec):
            if not lap_boundaries: return 1
            for end_time, lap_num in lap_boundaries:
                if sec <= end_time: return lap_num
            return len(lap_boundaries)

        details = raw_data.get('activityDetailMetrics', [])
        parsed_data = []
        prev_dist, prev_time = 0.0, 0.0
        
        for row in details:
            metrics = row.get('metrics', [])
            sum_time = get_val(metrics, 'sumElapsedDuration')
            if sum_time is None: continue 
                
            current_sec = int(sum_time) 
            hr = get_val(metrics, 'directHeartRate')
            sum_dist = get_val(metrics, 'sumDistance')
            
            speed_mps = None
            if sum_dist is not None:
                d_dist, d_time = sum_dist - prev_dist, sum_time - prev_time
                speed_mps = d_dist / d_time if d_time > 0 else 0.0
                prev_dist, prev_time = sum_dist, sum_time
                
            cadence = get_val(metrics, 'directRunCadence') or get_val(metrics, 'directDoubleCadence')
            if cadence and get_val(metrics, 'directRunCadence') is None: cadence /= 2 
            if cadence and cadence < 120: cadence *= 2
                
            elevation = get_val(metrics, 'directElevation')

            parsed_data.append({
                "Lap": get_lap(current_sec), "Second": current_sec, 
                "HeartRate": hr, "Speed_mps": speed_mps, 
                "Cadence": cadence, "Elevation": elevation
            })

        df_raw = pd.DataFrame(parsed_data).ffill()
        df_raw['Pace'] = np.where((df_raw['Speed_mps'] > 0.5), 26.8224 / df_raw['Speed_mps'], np.nan)

        df_raw['IntervalBlock'] = df_raw['Second'] // downsample_sec
        df_ai = df_raw.groupby('IntervalBlock').agg({
            'Lap': 'first', 'Second': 'first', 'HeartRate': 'mean', 
            'Speed_mps': 'mean', 'Cadence': 'mean',         
            'Elevation': lambda x: x.dropna().iloc[-1] - x.dropna().iloc[0] if len(x.dropna()) > 0 else 0  
        }).reset_index(drop=True).rename(columns={'Elevation': 'ElevationChange'})

        df_ai['HeartRate'] = pd.to_numeric(df_ai['HeartRate'], errors='coerce').round(0)
        df_ai['Cadence'] = pd.to_numeric(df_ai['Cadence'], errors='coerce').round(0)
        df_ai['ElevationChange'] = pd.to_numeric(df_ai['ElevationChange'], errors='coerce').round(1)
        
        def speed_to_pace_str(mps):
            if pd.isna(mps) or mps < 0.5: return "N/A"
            pace_min = 26.8224 / mps
            return f"{int(pace_min)}:{int((pace_min % 1) * 60):02d}"
            
        df_ai['Pace'] = df_ai['Speed_mps'].apply(speed_to_pace_str)
        return df_raw, df_ai[['Lap', 'Second', 'Pace', 'HeartRate', 'Cadence', 'ElevationChange']]

    def get_run_chat_history(self, activity_id):
        return self.load_json_safe(self.paths['manual'], f"run_{activity_id}_chat.json") or []

    def save_run_chat_message(self, activity_id, role, content):
        path = os.path.join(self.paths['manual'], f"run_{activity_id}_chat.json")
        history = self.get_run_chat_history(activity_id)
        history.append({"timestamp": datetime.datetime.now().isoformat(), "role": role, "content": content})
        with open(path, 'w') as f: json.dump(history, f, indent=4)

    # ==========================================
    # 🧬 RECOVERY & HEALTH DASHBOARD DATA
    # All shaping/aggregation for the Recovery & Health tab lives here.
    # dashboard.py should only call these functions and render.
    # ==========================================

    def _latest_available_date(self, folder_key: str, max_lookback: int = 7) -> str | None:
        """
        Return the most recent ISO date for which `folder_key/{date}.json` exists,
        searching backward from today. None if nothing within max_lookback days.
        """
        folder = self.paths.get(folder_key)
        if not folder or not os.path.isdir(folder):
            return None
        today = datetime.date.today()
        for i in range(max_lookback):
            d = (today - timedelta(days=i)).isoformat()
            if os.path.exists(os.path.join(folder, f"{d}.json")):
                return d
        return None

    def get_last_night_sleep(self) -> dict:
        """
        Last-night sleep details — stage minutes, respiration, sleep stress — plus
        7-day averages of each for comparison. Garmin names each night's file by
        wake date, so "last night" is today's file.

        Returns {} when no recent sleep file exists.
        """
        latest = self._latest_available_date('sleep')
        if not latest:
            return {}

        sleep = self.load_json_safe(self.paths['sleep'], f"{latest}.json")
        dto = sleep.get('dailySleepDTO', {}) or {}

        def _mins(key):
            val = dto.get(key)
            return round(val / 60) if val else 0

        current = {
            'date': latest,
            'deep_min': _mins('deepSleepSeconds'),
            'rem_min': _mins('remSleepSeconds'),
            'light_min': _mins('lightSleepSeconds'),
            'awake_min': _mins('awakeSleepSeconds'),
            'total_min': _mins('sleepTimeSeconds'),
            'avg_respiration': dto.get('averageRespirationValue'),
            'sleep_stress': dto.get('avgSleepStress'),  # nested under DTO, not top-level
        }

        # 7-day averages (excluding today) for delta comparisons
        today = datetime.date.fromisoformat(latest)
        samples = {k: [] for k in ['deep_min','rem_min','light_min','awake_min','total_min','avg_respiration','sleep_stress']}
        for i in range(1, 8):
            d = (today - timedelta(days=i)).isoformat()
            s = self.load_json_safe(self.paths['sleep'], f"{d}.json")
            if not s:
                continue
            sdto = s.get('dailySleepDTO', {}) or {}
            samples['deep_min'].append((sdto.get('deepSleepSeconds') or 0) / 60)
            samples['rem_min'].append((sdto.get('remSleepSeconds') or 0) / 60)
            samples['light_min'].append((sdto.get('lightSleepSeconds') or 0) / 60)
            samples['awake_min'].append((sdto.get('awakeSleepSeconds') or 0) / 60)
            samples['total_min'].append((sdto.get('sleepTimeSeconds') or 0) / 60)
            if sdto.get('averageRespirationValue') is not None:
                samples['avg_respiration'].append(sdto['averageRespirationValue'])
            if sdto.get('avgSleepStress') is not None:
                samples['sleep_stress'].append(sdto['avgSleepStress'])

        current['avg_7d'] = {
            k: (round(sum(v) / len(v), 1) if v else None) for k, v in samples.items()
        }
        return current

    def get_body_battery_series(self, days: int = 14) -> pd.DataFrame:
        """
        Daily Body Battery history from stats_and_body. One row per day with
        wake / lowest / most-recent / charged / drained values.
        """
        today = datetime.date.today()
        rows = []
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            stats = self.load_json_safe(self.paths['stats_body'], f"{d}.json")
            if not stats:
                continue
            rows.append({
                'date': d,
                'wake': stats.get('bodyBatteryAtWakeTime'),
                'lowest': stats.get('bodyBatteryLowestValue'),
                'highest': stats.get('bodyBatteryHighestValue'),
                'current': stats.get('bodyBatteryMostRecentValue'),
                'charged': stats.get('bodyBatteryChargedValue'),
                'drained': stats.get('bodyBatteryDrainedValue'),
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
        return df

    def get_training_readiness_today(self) -> dict:
        """
        Today's Training Readiness score, level, feedback, and the five factor
        percentages the Garmin algorithm uses. Returns {} if no recent file.
        """
        latest = self._latest_available_date('training_readiness')
        if not latest:
            return {}
        raw = self.load_json_safe(self.paths['training_readiness'], f"{latest}.json")
        entry = raw[0] if isinstance(raw, list) and raw else raw if isinstance(raw, dict) else {}
        if not entry:
            return {}
        return {
            'date': latest,
            'score': entry.get('score'),
            'level': entry.get('level'),
            'feedback_long': entry.get('feedbackLong'),
            'feedback_short': entry.get('feedbackShort'),
            'sleep_score': entry.get('sleepScore'),
            'recovery_time_min': entry.get('recoveryTime'),
            'hrv_weekly_avg': entry.get('hrvWeeklyAverage'),
            'factors': {
                'Sleep':                          entry.get('sleepScoreFactorPercent'),
                'Recovery Time':                  entry.get('recoveryTimeFactorPercent'),
                'HRV':                            entry.get('hrvFactorPercent'),
                'Acute:Chronic Workload Ratio':   entry.get('acwrFactorPercent'),
                'Stress History':                 entry.get('stressHistoryFactorPercent'),
            },
        }

    def _first_device_entry(self, device_map: dict) -> dict:
        """Garmin nests per-device data; grab the first (primary) device entry."""
        if not isinstance(device_map, dict) or not device_map:
            return {}
        return next(iter(device_map.values()), {}) or {}

    def get_training_status_today(self) -> dict:
        """
        Training Status snapshot: status phrase, ACWR ratio+status, VO2 max,
        heat acclimation percent. {} if no recent file.
        """
        latest = self._latest_available_date('training_status')
        if not latest:
            return {}
        raw = self.load_json_safe(self.paths['training_status'], f"{latest}.json")
        # Garmin nests: mostRecentTrainingStatus.latestTrainingStatusData[deviceId]
        status_map = ((raw.get('mostRecentTrainingStatus') or {})
                      .get('latestTrainingStatusData') or {})
        entry = self._first_device_entry(status_map)
        acute = entry.get('acuteTrainingLoadDTO') or {}
        vo2 = (raw.get('mostRecentVO2Max') or {}).get('generic') or {}
        heat = (raw.get('mostRecentVO2Max') or {}).get('heatAltitudeAcclimation') or {}
        return {
            'date': latest,
            'status_feedback': entry.get('trainingStatusFeedbackPhrase'),
            'status_code': entry.get('trainingStatus'),
            'fitness_trend': entry.get('fitnessTrend'),
            'acwr_percent': acute.get('acwrPercent'),
            'acwr_status': acute.get('acwrStatus'),
            'acwr_ratio': acute.get('dailyAcuteChronicWorkloadRatio'),
            'vo2_max': vo2.get('vo2MaxPreciseValue') or vo2.get('vo2MaxValue'),
            'heat_acclimation_pct': heat.get('heatAcclimationPercentage'),
        }

    def get_vo2_max_series(self, days: int = 30) -> pd.DataFrame:
        """
        VO2 Max history from training_status files. Garmin only writes a new
        value when it re-estimates, so rows are sparse — we forward-fill so
        the chart is a continuous line.
        """
        today = datetime.date.today()
        rows = []
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            raw = self.load_json_safe(self.paths['training_status'], f"{d}.json")
            if not raw:
                continue
            vo2 = (raw.get('mostRecentVO2Max') or {}).get('generic') or {}
            val = vo2.get('vo2MaxPreciseValue') or vo2.get('vo2MaxValue')
            if val is None:
                continue
            rows.append({'date': d, 'vo2_max': val})
        df = pd.DataFrame(rows)
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
        return df

    def get_weekly_intensity(self) -> dict:
        """
        This week's intensity-minute totals and goal. Uses today's file.
        """
        latest = self._latest_available_date('intensity_min')
        if not latest:
            return {}
        raw = self.load_json_safe(self.paths['intensity_min'], f"{latest}.json")
        if not raw:
            return {}
        moderate = raw.get('weeklyModerate') or 0
        vigorous = raw.get('weeklyVigorous') or 0
        # WHO guideline: vigorous minutes count double toward a moderate-equivalent total
        total = raw.get('weeklyTotal') or (moderate + vigorous * 2)
        goal = raw.get('weekGoal') or 150
        return {
            'date': latest,
            'moderate_min': moderate,
            'vigorous_min': vigorous,
            'total_min': total,
            'goal_min': goal,
            'percent': round(100 * total / goal) if goal else 0,
        }

    def get_fitness_age(self) -> dict:
        """Current Garmin fitness-age estimate, plus what's achievable."""
        latest = self._latest_available_date('fitness_age')
        if not latest:
            return {}
        raw = self.load_json_safe(self.paths['fitness_age'], f"{latest}.json")
        if not raw:
            return {}
        return {
            'chronological': raw.get('chronologicalAge'),
            'fitness': raw.get('fitnessAge'),
            'achievable': raw.get('achievableFitnessAge'),
            'previous': raw.get('previousFitnessAge'),
        }

    # Human-readable translations of Garmin's cryptic status codes.
    # Kept here so the dashboard never has to interpret raw API strings.
    TRAINING_STATUS_LABELS = {
        0: "No Status", 1: "Detraining", 2: "Unproductive", 3: "Recovery",
        4: "Maintaining", 5: "Productive", 6: "Peaking", 7: "Overreaching",
    }
    READINESS_FEEDBACK_TEXT = {
        "BOOSTED_BY_GOOD_SLEEP": "Boosted by good sleep",
        "LIMITED_BY_POOR_SLEEP": "Limited by poor sleep",
        "LIMITED_BY_HIGH_STRESS": "Limited by high stress",
        "LIMITED_BY_LOW_HRV": "Limited by low HRV",
        "LIMITED_BY_RECOVERY": "Still recovering",
        "GOOD_RECOVERY": "Good recovery",
    }

    def describe_training_status(self, code: int | None) -> str:
        return self.TRAINING_STATUS_LABELS.get(code, "Unknown") if code is not None else "Unknown"

    def describe_readiness_feedback(self, short: str | None, long: str | None) -> str:
        """Prefer a clean known translation; fall back to Garmin's raw token."""
        if short and short in self.READINESS_FEEDBACK_TEXT:
            return self.READINESS_FEEDBACK_TEXT[short]
        return (long or short or "").replace("_", " ").title()