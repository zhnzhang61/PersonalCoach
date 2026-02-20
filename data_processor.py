import os
import json
import math
import datetime
import csv
import statistics
from datetime import timedelta

class DataProcessor:
    def __init__(self, data_dir='data'):
        self.data_dir = data_dir
        self.paths = {
            'activities': os.path.join(data_dir, 'get_activities'),
            'splits': os.path.join(data_dir, 'get_activity_splits'),
            'hr_zones': os.path.join(data_dir, 'get_activity_hr_in_timezones'),
            'manual': os.path.join(data_dir, 'manual_inputs'),
            'blocks': os.path.join(data_dir, 'blocks', 'training_blocks.json'),
            'aux': os.path.join(data_dir, 'blocks', 'auxiliary_log.json'),
            # --- NEW HEALTH PATHS ---
            'sleep': os.path.join(data_dir, 'get_sleep_data'),
            'rhr': os.path.join(data_dir, 'get_rhr_day'),
            'hrv': os.path.join(data_dir, 'get_hrv_data'),
            'stress': os.path.join(data_dir, 'get_stress_data'),
            'ledger': os.path.join(data_dir, 'derived', 'daily_health_metrics.csv')
        }
        self._ensure_infrastructure()

    def _ensure_infrastructure(self):
        os.makedirs(os.path.dirname(self.paths['blocks']), exist_ok=True)
        os.makedirs(os.path.dirname(self.paths['ledger']), exist_ok=True) # Ensure derived folder exists
        
        if not os.path.exists(self.paths['blocks']):
            default_block = [{
                "id": "block_001",
                "name": "Spring 2026 Build",
                "start_date": "2025-12-25",
                "end_date": "2026-04-19",
                "primary_event": "running",
                "baseline_snapshot": {"period": "N/A", "note": "Baseline"}
            }]
            with open(self.paths['blocks'], 'w') as f: json.dump(default_block, f, indent=4)
        if not os.path.exists(self.paths['aux']):
            with open(self.paths['aux'], 'w') as f: json.dump([], f)
        os.makedirs(self.paths['manual'], exist_ok=True)

    def load_json_safe(self, folder, filename):
        """Helper to safely load JSON files."""
        try:
            path = os.path.join(folder, filename)
            if not os.path.exists(path): return {}
            with open(path, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    # --- NEW: HEALTH AGGREGATION LOGIC ---

    def compile_health_ledger(self, days_back=120):
        """
        Aggregates disparate JSON files into a single CSV timeline.
        """
        records = []
        today = datetime.date.today()
        
        # Pre-scan activities to avoid opening thousands of files inside the loop
        # This creates a map of date -> {dist, time}
        activity_map = {}
        if os.path.exists(self.paths['activities']):
            for f in os.listdir(self.paths['activities']):
                if f.endswith('_summary.json'):
                    act = self.load_json_safe(self.paths['activities'], f)
                    if not act: continue
                    
                    # Extract date YYYY-MM-DD
                    raw_date = act.get('startTimeLocal')
                    if not raw_date: continue
                    d_str = raw_date[:10]
                    
                    if d_str not in activity_map:
                        activity_map[d_str] = {'dist': 0, 'time': 0}
                    
                    activity_map[d_str]['dist'] += act.get('distance', 0)
                    activity_map[d_str]['time'] += act.get('duration', 0)

        for i in range(days_back):
            date_obj = today - timedelta(days=i)
            date_str = date_obj.isoformat()
            
            # 1. Fetch Raw Data
            sleep = self.load_json_safe(self.paths['sleep'], f"{date_str}.json")
            rhr = self.load_json_safe(self.paths['rhr'], f"{date_str}.json")
            hrv = self.load_json_safe(self.paths['hrv'], f"{date_str}.json")
            stress = self.load_json_safe(self.paths['stress'], f"{date_str}.json")
            
            # 2. Extract Key Metrics (Safely)
            # Sleep
            sleep_dto = sleep.get('dailySleepDTO', {})
            sleep_score = sleep_dto.get('sleepScores', {}).get('overall', {}).get('value')
            sleep_sec = sleep_dto.get('sleepTimeSeconds', 0)
            
            # RHR
            rhr_metrics = rhr.get('allMetrics', {}).get('metricsMap', {}).get('WELLNESS_RESTING_HEART_RATE', [])
            rhr_val = rhr_metrics[0].get('value') if rhr_metrics else None
            
            # HRV (Nightly Avg)
            hrv_val = hrv.get('hrvSummary', {}).get('weeklyAvg') # Fallback
            if hrv.get('hrvData'): # Prefer last night
                hrv_val = hrv.get('hrvData', {}).get('lastNightAvg')

            # Stress
            stress_val = stress.get('avgStressLevel')

            # 3. Training Load from Map
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

        # Sort Chronologically
        records.sort(key=lambda x: x['date'])

        # Write to CSV
        if records:
            keys = records[0].keys()
            with open(self.paths['ledger'], 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(records)
        
        return records

    def get_health_stats(self):
        """
        Reads the ledger.
        """
        if not os.path.exists(self.paths['ledger']):
            return self.compile_health_ledger()

        data = []
        try:
            with open(self.paths['ledger'], 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Type conversion
                    for k, v in row.items():
                        if k != 'date':
                            row[k] = float(v) if v and v != 'None' else None
                    data.append(row)
        except Exception:
            return self.compile_health_ledger()

        return data

    # --- EXISTING METHODS BELOW ---

    def get_blocks(self):
        with open(self.paths['blocks'], 'r') as f: return json.load(f)

    def get_weeks_for_block(self, block_id):
        blocks = self.get_blocks()
        block = next((b for b in blocks if b['id'] == block_id), None)
        if not block: return []
        start = datetime.date.fromisoformat(block['start_date'])
        end = datetime.date.fromisoformat(block['end_date'])
        weeks = []
        
        if start.isoformat() == "2025-12-25":
            weeks.append({"week_num": 0, "start": "2025-12-25", "end": "2025-12-27", "label": "Week 0 (Short)"})
            weeks.append({"week_num": 1, "start": "2025-12-28", "end": "2026-01-04", "label": "Week 1 (Bridge)"})
            curr = datetime.date(2026, 1, 5)
            week_num = 2
        else:
            days_until_sunday = 6 - start.weekday() 
            w0_end = min(start + timedelta(days=days_until_sunday), end)
            weeks.append({"week_num": 0, "start": start.isoformat(), "end": w0_end.isoformat(), "label": f"Week 0"})
            curr = w0_end + timedelta(days=1)
            week_num = 1

        while curr <= end:
            w_end = min(curr + timedelta(days=6), end)
            weeks.append({"week_num": week_num, "start": curr.isoformat(), "end": w_end.isoformat(), "label": f"Week {week_num} ({curr.strftime('%b %d')})"})
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
                # We need to distinguish between raw API files and manually saved _summary.json files
                # The user's previous code seemed to iterate all .json
                # We'll stick to the safe loading
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

    def get_run_laps(self, activity_id):
            """Fetches laps and discards the last one if duration < 10 seconds."""
            json_path = os.path.join(self.paths['splits'], f"{activity_id}.json")
            if not os.path.exists(json_path):
                return []
            
            try:
                with open(json_path, 'r') as f:
                    data = json.load(f)
                    laps = data.get('lapDTOs', [])
                    if laps and laps[-1].get('duration', 0) < 10:
                        laps.pop()
                    return laps
            except Exception as e:
                print(f"Error reading splits: {e}")
                return []

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

    def save_run_metadata(self, activity_id, week_num, run_name, category_stats):
        meta = {
            "name": run_name,
            "week_num": week_num,
            "category_stats": category_stats,
            "updated_at": datetime.datetime.now().isoformat()
        }
        with open(os.path.join(self.paths['manual'], f"run_{activity_id}_meta.json"), 'w') as f:
            json.dump(meta, f, indent=4)

    def add_aux_activity(self, date_str, event_type, desc):
        with open(self.paths['aux'], 'r') as f: current = json.load(f)
        current.append({"id": f"aux_{int(datetime.datetime.now().timestamp())}", "date": date_str, "type": event_type, "desc": desc})
        with open(self.paths['aux'], 'w') as f: json.dump(current, f, indent=4)

    def get_aux_in_range(self, start_str, end_str):
        with open(self.paths['aux'], 'r') as f: current = json.load(f)
        return [x for x in current if start_str <= x['date'] <= end_str]

    def build_ai_context(self, activity_id, block_id):
        meta_path = os.path.join(self.paths['manual'], f"run_{activity_id}_meta.json")
        garmin_hr_path = os.path.join(self.paths['hr_zones'], f"{activity_id}.json")
        custom_hr_path = os.path.join(self.paths['manual'], "user_zones.json")

        if not os.path.exists(meta_path): return None
        with open(meta_path) as f: run_meta = json.load(f)
        
        hr_zones_display = []
        if os.path.exists(custom_hr_path):
            try:
                with open(custom_hr_path) as f:
                    custom_zones = json.load(f)
                    for name, range_str in custom_zones.items():
                        hr_zones_display.append({"name": name, "range": range_str})
            except Exception as e: print(f"Error reading user_zones.json: {e}")

        if not hr_zones_display and os.path.exists(garmin_hr_path):
            try:
                with open(garmin_hr_path) as f: 
                    garmin_zones = json.load(f)
                    for z in garmin_zones:
                        if 'zoneLowBoundary' in z:
                            hr_zones_display.append({
                                "name": f"Zone {z['zoneNumber']}",
                                "range": f">{z['zoneLowBoundary']} bpm"
                            })
            except: pass

        run_date = datetime.date.today().isoformat()
        for f in os.listdir(self.paths['activities']):
            if not f.endswith('.json'): continue
            try:
                with open(os.path.join(self.paths['activities'], f)) as jf:
                    data = json.load(jf)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if str(item.get('activityId')) == str(activity_id):
                            run_date = item.get('startTimeLocal')[:10]
                            break
            except: pass

        block = next((b for b in self.get_blocks() if b['id'] == block_id), {})
        date_obj = datetime.date.fromisoformat(run_date)
        start_7 = (date_obj - timedelta(days=7)).isoformat()
        aux_events = self.get_aux_in_range(start_7, run_date)

        return {
            "block_goal": block.get('name'),
            "run_context": {
                "date": run_date,
                "user_name": run_meta.get('name'),
                "category_stats": run_meta.get('category_stats'),
                "hr_zones": hr_zones_display
            },
            "auxiliary_activities_last_7d": aux_events
        }