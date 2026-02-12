import os
import time
import json
import inspect
import datetime
import sys
import subprocess
from dotenv import load_dotenv

# --- Dependency Check ---
required = {'garminconnect', 'python-dotenv'}
installed = set()
try:
    import pkg_resources
    installed = {pkg.key for pkg in pkg_resources.working_set}
except ImportError:
    pass

missing = required - installed
if missing:
    print(f"Installing missing dependencies: {missing}")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', *missing])

from garminconnect import Garmin
load_dotenv()

class GarminSync:
    def __init__(self, email, password, data_dir='data'):
        self.email = email
        self.password = password
        self.client = None
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.daily_methods, self.static_methods, self.activity_methods = [], [], []
        self.range_methods, self.special_methods = [], [] 

    def connect(self):
        try:
            self.client = Garmin(self.email, self.password)
            self.client.login()
            print(f"✅ Login successful.")
            self._introspect_api()
            return True
        except Exception as err:
            print(f"❌ Login Error: {err}")
            return False

    def _introspect_api(self):
        print("🔍 Scanning API capabilities...")
        EXCLUDE = ['get_activities', 'get_activities_by_date', 'download_activity', 'get_weekly_intensity_minutes']
        SPECIAL = ['get_lactate_threshold', 'get_race_predictions']

        for name in dir(self.client):
            if not name.startswith('get_') or name in EXCLUDE: continue
            attr = getattr(self.client, name)
            if not callable(attr): continue
            
            if name in SPECIAL: # Group 6
                self.special_methods.append(name)
                continue

            try:
                sig = inspect.signature(attr)
                params = list(sig.parameters.values())
                req = [p for p in params if p.default == inspect.Parameter.empty]
                
                # Group 1: Static (0 args)
                if len(req) == 0:
                    self.static_methods.append(name)
                # Group 2: Daily (1 arg: date/cdate/fordate)
                elif len(req) == 1 and req[0].name in ['cdate', 'date', 'day', 'iso_date', 'fordate']:
                    self.daily_methods.append((name, req[0].name))
                # Group 3: Activity (1 arg: activity_id)
                elif len(req) == 1 and req[0].name in ['activity_id', 'id']:
                    self.activity_methods.append(name)
                # Group 4: Range (2 args: start/end)
                elif len(req) == 2 and {req[0].name, req[1].name} <= {'start', 'end', 'startdate', 'enddate'}:
                    self.range_methods.append((name, req[0].name, req[1].name))
                # Group 5: Pagination (Ignored)
            except ValueError: continue
        
        print(f"   Mapped: {len(self.daily_methods)} Daily, {len(self.static_methods)} Static, "
              f"{len(self.activity_methods)} Activity, {len(self.range_methods)} Range, {len(self.special_methods)} Special.")

    def _save(self, data, method_name, filename):
        if not data: return
        folder_path = os.path.join(self.data_dir, method_name)
        os.makedirs(folder_path, exist_ok=True)
        if not os.path.exists(os.path.join(folder_path, filename)):
            with open(os.path.join(folder_path, filename), 'w') as f:
                json.dump(data, f, indent=4, default=str)

    def run_sync(self, days_back=7, activity_limit=20):
        today = datetime.date.today().isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

        # Group 1: Static
        print("⬇️ Syncing Global/Static Data...")
        for method in self.static_methods:
            try:
                print(f"   > Calling: {method}()...")
                self._save(getattr(self.client, method)(), method, "latest.json")
            except: pass

        # Group 6: Special
        print("⬇️ Syncing Special Data...")
        try:
            if 'get_lactate_threshold' in self.special_methods:
                print("   > Calling: get_lactate_threshold...")
                self._save(self.client.get_lactate_threshold(latest=False, start_date=yesterday, end_date=today), 
                           'get_lactate_threshold', 'latest.json')
            if 'get_race_predictions' in self.special_methods:
                print("   > Calling: get_race_predictions...")
                self._save(self.client.get_race_predictions(startdate=yesterday, enddate=today), 
                           'get_race_predictions', 'latest.json')
        except Exception as e: print(f"   ⚠️ Special failed: {e}")

        # Group 4: Range
        print("⬇️ Syncing Range Data (Yesterday -> Today)...")
        for method, k1, k2 in self.range_methods:
            try:
                print(f"   > Calling: {method}({yesterday}, {today})...")
                self._save(getattr(self.client, method)(**{k1: yesterday, k2: today}), method, f"{yesterday}_to_{today}.json")
            except: pass

        # Group 2: Daily
        print(f"⬇️ Syncing Daily Data ({days_back} days)...")
        for i in range(days_back):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            for method, arg_name in self.daily_methods:
                try:
                    if not os.path.exists(os.path.join(self.data_dir, method, f"{d}.json")):
                        print(f"   > Calling: {method}({d})...")
                        self._save(getattr(self.client, method)(**{arg_name: d}), method, f"{d}.json")
                        time.sleep(0.05)
                except: pass
        
        # Group 3: Activities
        print(f"\n⬇️ Syncing Activities ({activity_limit})...")
        try:
            print(f"   > Calling: get_activities(0, {activity_limit})...")
            activities = self.client.get_activities(0, activity_limit)
        except Exception as e:
            print(f"❌ Failed to get activity list: {e}"); return

        for index, act in enumerate(activities):
            act_id = act['activityId']
            act_name = act['activityName']
            print(f"   [{index+1}/{len(activities)}] Processing {act_name} ({act_id})...")
            self._save(act, "get_activities", f"{act_id}_summary.json")

            for method in self.activity_methods:
                if not os.path.exists(os.path.join(self.data_dir, method, f"{act_id}.json")):
                    try:
                        print(f"      > Calling: {method}({act_id})...")
                        self._save(getattr(self.client, method)(act_id), method, f"{act_id}.json")
                        time.sleep(0.1) 
                    except: pass

if __name__ == "__main__":
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASS")
    if not email or not password: print("⚠️ Credentials not found.")
    else:
        syncer = GarminSync(email, password)
        if syncer.connect(): syncer.run_sync(days_back=20, activity_limit=50)