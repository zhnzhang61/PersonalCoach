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
        
        # We do NOT pre-define folders. They are created dynamically based on method names.
        os.makedirs(self.data_dir, exist_ok=True)

        self.daily_methods = []
        self.static_methods = []
        self.activity_methods = [] 

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
        """
        Dynamically categorize API methods based on their signature.
        """
        print("🔍 Scanning API capabilities...")
        names = dir(self.client);
        for name in names:
            if not name.startswith('get_'): continue
            
            attr = getattr(self.client, name)
            if not callable(attr): continue

            try:
                sig = inspect.signature(attr)
                params = list(sig.parameters.values())
                
                # 1. Static (No args)
                if len(params) == 0:
                    self.static_methods.append(name)
                
                # 2. Daily (Date arg)
                elif len(params) == 1 and params[0].name in ['cdate', 'date', 'day']:
                    self.daily_methods.append(name)
                
                # 3. Activity Specific (Activity ID arg)
                # Exclude 'get_activities' itself to avoid recursion (it takes range args)
                #elif len(params) == 1 and params[0].name in ['activity_id', 'id']:
                elif len(params) == 1 and params[0].name in ['activity_id', 'id']:
                    self.activity_methods.append(name)
                    
            except ValueError:
                continue
        
        print(f"   Found {len(self.daily_methods)} daily, {len(self.static_methods)} static, and {len(self.activity_methods)} per-activity methods.")

    def _save(self, data, method_name, filename):
        """
        Saves data to data/{method_name}/{filename}
        """
        if not data: return
        
        # Strict Rule: Folder name = Method name, directly under data_dir
        folder_path = os.path.join(self.data_dir, method_name)
        os.makedirs(folder_path, exist_ok=True)
        
        filepath = os.path.join(folder_path, filename)
        
        # Optimization: Only write if file doesn't exist (Smart Sync)
        # Remove this check if you want purely destructive overwrite
        if not os.path.exists(filepath):
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=4, default=str)

    def run_sync(self, days_back=7, activity_limit=20):
        # 1. Static Methods -> data/get_device_settings/static.json
        print("⬇️ Syncing Static Data...")
        for method in self.static_methods:
            try:
                self._save(getattr(self.client, method)(), method, "static.json")
            except: pass

        # 2. Daily Methods -> data/get_user_summary/2023-10-27.json
        print(f"⬇️ Syncing Daily Data ({days_back} days)...")
        today = datetime.date.today()
        for i in range(days_back):
            date_str = (today - datetime.timedelta(days=i)).isoformat()
            for method in self.daily_methods:
                try:
                    # Check existence before call to save API hits
                    target = os.path.join(self.data_dir, method, f"{date_str}.json")
                    if not os.path.exists(target):
                        self._save(getattr(self.client, method)(date_str), method, f"{date_str}.json")
                        time.sleep(0.05) 
                except: pass
        
        # 3. Activities
        print(f"\n⬇️ Syncing Activities ({activity_limit})...")
        try:
            # We explicitly handle get_activities to fetch the list
            activities = self.client.get_activities(0, activity_limit)
        except Exception as e:
            print(f"❌ Failed to get activity list: {e}")
            return

        for index, act in enumerate(activities):
            act_id = act['activityId']
            act_name = act['activityName']
            print(f"   [{index+1}/{len(activities)}] Processing {act_name} ({act_id})...")

            # Save Summary -> data/get_activities/{id}_summary.json
            self._save(act, "get_activities", f"{act_id}_summary.json")

            # 4. Dynamic Activity Data -> data/get_activity_splits/{id}.json
            for method in self.activity_methods:
                target = os.path.join(self.data_dir, method, f"{act_id}.json")
                if not os.path.exists(target):
                    try:
                        data = getattr(self.client, method)(act_id)
                        self._save(data, method, f"{act_id}.json")
                        time.sleep(0.1) 
                    except Exception: 
                        pass

if __name__ == "__main__":
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASS")
    
    if not email or not password:
        print("⚠️ Credentials not found.")
    else:
        syncer = GarminSync(email, password)
        if syncer.connect():
            syncer.run_sync(days_back=10, activity_limit=20)