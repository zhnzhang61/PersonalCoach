import os
import json
import math
import datetime
from datetime import timedelta

class DataProcessor:
    def __init__(self, data_dir='data'):
        self.data_dir = data_dir
        self.paths = {
            'activities': os.path.join(data_dir, 'get_activities'),
            'splits': os.path.join(data_dir, 'get_activity_splits'), # Primary Source now
            'hr_zones': os.path.join(data_dir, 'get_activity_hr_in_timezones'),
            'manual': os.path.join(data_dir, 'manual_inputs'),
            'blocks': os.path.join(data_dir, 'blocks', 'training_blocks.json'),
            'aux': os.path.join(data_dir, 'blocks', 'auxiliary_log.json')
        }
        self._ensure_infrastructure()

    def _ensure_infrastructure(self):
        os.makedirs(os.path.dirname(self.paths['blocks']), exist_ok=True)
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

    # --- NEW: LAP BASED LOGIC ---
    
    def get_run_laps(self, activity_id):
            """Fetches laps and discards the last one if duration < 10 seconds."""
            json_path = os.path.join(self.paths['splits'], f"{activity_id}.json")
            if not os.path.exists(json_path):
                return []
            
            try:
                with open(json_path, 'r') as f:
                    data = json.load(f)
                    laps = data.get('lapDTOs', [])
                    
                    # Apply Rule: Discard last lap if duration < 10s
                    if laps and laps[-1].get('duration', 0) < 10:
                        laps.pop()
                        
                    return laps
            except Exception as e:
                print(f"Error reading splits: {e}")
                return []

    def calculate_category_stats(self, labeled_laps):
        """
        Groups laps by user category using the specific weighted formulas provided.
        """
        groups = {}
        # Approved categories only
        valid_cats = ["Hold Back Easy", "Steady Effort", "Increasing Effort", "Marathon", "LT Effort", "VO2Max", "Sprint", "Rest"]
        
        for lap in labeled_laps:
            cat = lap.get('category', 'Rest')
            if cat not in valid_cats: cat = "Rest"
            
            if cat not in groups:
                groups[cat] = {'total_dist': 0.0, 'total_time': 0.0, 'weighted_hr_sum': 0.0}
            
            dist = lap.get('distance', 0)    # meters
            dur = lap.get('duration', 0)      # seconds
            hr = lap.get('averageHR', 0)
            
            groups[cat]['total_dist'] += dist
            groups[cat]['total_time'] += dur
            groups[cat]['weighted_hr_sum'] += (hr * dist)

        results = []
        for cat, stats in groups.items():
            t_dist = stats['total_dist']
            t_time = stats['total_time']
            
            # Pace: Total Dist / Total Time -> min/mile
            pace_str = "N/A"
            if t_dist > 0 and t_time > 0:
                speed_mps = t_dist / t_time
                pace_decimal = (1609.34 / speed_mps) / 60
                pace_str = f"{int(pace_decimal)}:{int((pace_decimal % 1) * 60):02d}"

            # Weighted HR: Sum(HR * dist) / Total Dist
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
            hr_path = os.path.join(self.paths['hr_zones'], f"{activity_id}.json")

            if not os.path.exists(meta_path): return None
            with open(meta_path) as f: run_meta = json.load(f)
            
            # Pull HR Zones
            hr_zones = []
            if os.path.exists(hr_path):
                try:
                    with open(hr_path) as f: hr_zones = json.load(f)
                except: pass

            # Find the date from the activity summary
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

            # Get Block Info
            block = next((b for b in self.get_blocks() if b['id'] == block_id), {})
            
            # Get Fatigue Log
            date_obj = datetime.date.fromisoformat(run_date)
            start_7 = (date_obj - timedelta(days=7)).isoformat()
            aux_events = self.get_aux_in_range(start_7, run_date)

            return {
                "block_goal": block.get('name'),
                "run_context": {
                    "date": run_date,
                    "user_name": run_meta.get('name'),
                    "category_stats": run_meta.get('category_stats'), # The new summary
                    "hr_zones": hr_zones
                },
                "auxiliary_activities_last_7d": aux_events
            }