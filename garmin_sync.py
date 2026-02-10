import os
import time
import json
import inspect
import datetime
import sys
import subprocess

# --- Dependency Check ---
required = {'garminconnect', 'fitdecode', 'python-dotenv'}
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
from dotenv import load_dotenv

load_dotenv()

class GarminSync:
    def __init__(self, email, password, data_dir='data'):
        self.email = email
        self.password = password
        self.client = None
        self.data_dir = data_dir
        
        self.folders = {
            'activities': os.path.join(data_dir, 'activities'),
            'manual': os.path.join(data_dir, 'manual_inputs'),
            'blocks': os.path.join(data_dir, 'blocks'),
            'daily': os.path.join(data_dir, 'daily_metrics'),
            'daily_details': os.path.join(data_dir, 'daily_metrics', 'details')
        }
        for f in self.folders.values():
            os.makedirs(f, exist_ok=True)

        self.daily_methods = []
        self.static_methods = []

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
        for name in dir(self.client):
            attr = getattr(self.client, name)
            if name.startswith('get_') and callable(attr):
                try:
                    sig = inspect.signature(attr)
                    params = list(sig.parameters.values())
                    if len(params) == 0:
                        self.static_methods.append(name)
                    elif len(params) == 1 and params[0].name in ['cdate', 'date', 'day']:
                        self.daily_methods.append(name)
                except ValueError:
                    continue

    def _save(self, data, folder_sub, filename):
        if not data: return
        folder = os.path.join(self.folders['daily_details'], folder_sub)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, filename), 'w') as f:
            json.dump(data, f, indent=4, default=str)

    def run_sync(self, days_back=7, activity_limit=20):
        """
        Main sync function.
        Increase activity_limit (e.g., 100) to fetch older missing runs.
        """
        print("⬇️ Syncing Static Data...")
        for method in self.static_methods:
            try:
                self._save(getattr(self.client, method)(), 'static', f"{method}.json")
            except: pass

        print(f"⬇️ Syncing Daily Data ({days_back} days)...")
        today = datetime.date.today()
        for i in range(days_back):
            date_str = (today - datetime.timedelta(days=i)).isoformat()
            for method in self.daily_methods:
                path = os.path.join(self.folders['daily_details'], method, f"{date_str}.json")
                # Incremental check: Only fetch if missing
                if not os.path.exists(path):
                    try:
                        self._save(getattr(self.client, method)(date_str), method, f"{date_str}.json")
                        time.sleep(0.01) 
                    except: pass
        
        print(f"\n⬇️ Syncing Activities ({activity_limit})...")
        activities = self.client.get_activities(0, activity_limit)
        for act in activities:
            act_id = act['activityId']
            act_name = act['activityName']
            fit_path = os.path.join(self.folders['activities'], f"{act_id}.fit")
            
            # Save Summary JSON
            with open(os.path.join(self.folders['activities'], f"{act_id}_summary.json"), 'w') as f:
                json.dump(act, f, indent=4)

            # Download .fit if missing
            if not os.path.exists(fit_path):
                print(f"   Downloading .fit: {act_name} ({act_id})")
                try:
                    zip_data = self.client.download_activity(act_id, dl_fmt=self.client.ActivityDownloadFormat.ORIGINAL)
                    with open(fit_path, "wb") as fb:
                        fb.write(zip_data)
                except Exception as e:
                    print(f"   Failed {act_id}: {e}")

if __name__ == "__main__":
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASS")
    
    if not email or not password:
        print("⚠️ Credentials not found.")
    else:
        syncer = GarminSync(email, password)
        if syncer.connect():
            # NOTE: If you are missing old runs, change 20 to 100 below!
            syncer.run_sync(days_back=20, activity_limit=100)