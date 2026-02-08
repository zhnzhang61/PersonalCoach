import os
import time
import json
import inspect
import datetime
import traceback
from garminconnect import Garmin

class GarminSync:
    def __init__(self, email, password, data_dir='data'):
        self.email = email
        self.password = password
        self.client = None
        self.data_dir = data_dir
        
        # We will populate these dynamically after login
        self.daily_methods = []   # Methods that take (self, cdate)
        self.static_methods = []  # Methods that take (self)
        self.range_methods = []   # Methods that take start/end or similar
        
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)

    def connect(self):
        """authenticate and then INSPECT the client to find capability."""
        try:
            self.client = Garmin(self.email, self.password)
            self.client.login()
            print(f"✅ Login successful.")
            
            # --- DYNAMIC METHOD DISCOVERY ---
            self._introspect_api()
            return True
        except Exception as err:
            print(f"❌ Login/Init Error: {err}")
            return False

    def _introspect_api(self):
        """
        Reflects on the Garmin object to find ALL 'get_' methods.
        Classifies them based on their function signature.
        """
        print("\n🔍 Scanning API capabilities...")
        all_attributes = dir(self.client)
        methods = [m for m in all_attributes if m.startswith('get_') or m.startswith('download_')]
        
        for method_name in methods:
            # Get the actual function object
            method_ref = getattr(self.client, method_name)
            
            if not callable(method_ref):
                continue

            try:
                sig = inspect.signature(method_ref)
                params = list(sig.parameters.values())
                param_names = [p.name for p in params]

                # 1. STATIC METHODS: No arguments (except self, implicit)
                if len(params) == 0:
                    self.static_methods.append(method_name)
                
                # 2. DAILY METHODS: 1 argument, looks like a date
                elif len(params) == 1 and param_names[0] in ['cdate', 'date', 'day', 'iso_date']:
                    self.daily_methods.append(method_name)

                # 3. RANGE/WEEKLY METHODS: 2 arguments (likely start, end or date, period)
                # We won't auto-call these in the daily loop to avoid massive duplication, 
                # but we categorize them for specific handling (like Activities)
                elif len(params) >= 2 and ('start' in param_names or 'end' in param_names):
                    self.range_methods.append(method_name)

            except ValueError:
                # Built-ins might not have signatures
                pass

        print(f"   found {len(self.static_methods)} static methods (Profile, Settings, etc.)")
        print(f"   found {len(self.daily_methods)} daily methods (Sleep, HR, Steps, etc.)")
        print(f"   found {len(self.range_methods)} range methods (Activities, Ranges)")

    def _save(self, data, folder_name, filename):
        """Standardized Saver: data/method_name/filename"""
        if not data: return
        
        folder_path = os.path.join(self.data_dir, folder_name)
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
            
        path = os.path.join(folder_path, filename)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def run_comprehensive_sync(self, days_back=30, activity_limit=100):
        """
        The Master Sync Function.
        1. Calls all Static Methods.
        2. Iterates days and calls all Daily Methods.
        3. Specialized handling for Activities (which require ID extraction).
        """
        
        # --- PHASE 1: STATIC DATA ---
        print(f"\n[Phase 1] Downloading Static Data ({len(self.static_methods)} methods)...")
        for method_name in self.static_methods:
            # Safety: Don't re-download if we already have it (optional, but good for speed)
            # For static data, we might want to refresh, so we overwrite.
            try:
                func = getattr(self.client, method_name)
                print(f"   Calling {method_name}()...")
                data = func()
                self._save(data, method_name, "data.json")
            except Exception as e:
                print(f"   ⚠️ {method_name} failed: {str(e)[:50]}")


        # --- PHASE 2: DAILY DATA ---
        print(f"\n[Phase 2] Downloading Daily Data ({days_back} days)...")
        today = datetime.date.today()
        
        for i in range(days_back):
            date_obj = today - datetime.timedelta(days=i)
            date_str = date_obj.isoformat()
            
            # Optimization: Check if we have *some* data for this date to avoid printing too much
            # But we must check EVERY method individually.
            
            for method_name in self.daily_methods:
                # Check if file exists to prevent duplicate API calls
                folder_path = os.path.join(self.data_dir, method_name)
                file_path = os.path.join(folder_path, f"{date_str}.json")
                
                if os.path.exists(file_path):
                    continue # Skip if already downloaded

                try:
                    func = getattr(self.client, method_name)
                    # print(f"   Calling {method_name}({date_str})...") # Verbose
                    data = func(date_str)
                    if data:
                        self._save(data, method_name, f"{date_str}.json")
                        time.sleep(0.1) # Micro-pause to prevent rate limits
                except Exception as e:
                    # Many methods return 404 or 500 if data is missing for that specific day
                    # This is normal, so we silence it or log minimally
                    pass
            
            print(f"   Processed {date_str} (Checked {len(self.daily_methods)} endpoints)", end='\r')


        # --- PHASE 3: ACTIVITIES (Special Handling) ---
        # Activities are unique because we need the List first, then we use IDs to get details/files.
        print(f"\n\n[Phase 3] Downloading Activities & Details...")
        
        # 3a. Get Activity List
        # We assume get_activities(start, limit) exists based on API knowledge, 
        # or we find the method in range_methods that looks like it.
        try:
            # We explicitly use the known method for the list, as it drives the rest
            activities = self.client.get_activities(0, activity_limit)
            self._save(activities, "get_activities", "recent_list.json")
            
            for act in activities:
                act_id = act['activityId']
                act_type = act['activityType']['typeKey']
                
                # 3b. Download FIT File
                fit_folder = os.path.join(self.data_dir, "download_activity")
                if not os.path.exists(fit_folder): os.makedirs(fit_folder)
                
                fit_path = os.path.join(fit_folder, f"{act_id}.fit")
                
                if not os.path.exists(fit_path):
                    print(f"   Downloading .fit for {act_type} {act_id}...")
                    try:
                        # Note: 'download_activity' is a specific method we know exists.
                        # We use ActivityDownloadFormat.ORIGINAL which is usually mapped to generic structure
                        zip_data = self.client.download_activity(act_id, dl_fmt=self.client.ActivityDownloadFormat.ORIGINAL)
                        with open(fit_path, "wb") as fb:
                            fb.write(zip_data)
                        time.sleep(1)
                    except Exception as e:
                        print(f"   Failed to download fit {act_id}: {e}")

                # 3c. Download JSON Details
                # Use introspection to find methods that might take an Activity ID?
                # Usually methods like 'get_activity_details' or 'get_activity_splits'
                # We hardcode these "ID-dependent" methods because they are hard to inspect automatically (int vs int)
                id_methods = ['get_activity_details', 'get_activity_splits', 'get_activity_hr_in_timezones']
                
                for id_method in id_methods:
                    if hasattr(self.client, id_method):
                        # Check exist
                        f_folder = os.path.join(self.data_dir, id_method)
                        f_path = os.path.join(f_folder, f"{act_id}.json")
                        
                        if not os.path.exists(f_path):
                            try:
                                func = getattr(self.client, id_method)
                                d = func(act_id)
                                self._save(d, id_method, f"{act_id}.json")
                            except: pass

        except Exception as e:
            print(f"Error in Activity Phase: {e}")

        print("\n✅ Sync Complete.")

if __name__ == "__main__":
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASS")

    if not email or not password:
        print("⚠️ Set GARMIN_EMAIL and GARMIN_PASS environment variables.")
    else:
        # User requested "Download all possible data once"
        # Set days_back to a high number (e.g., 365 or 1000) for the initial full dump.
        # Set activity_limit to 1000+ for full history.
        syncer = GarminSync(email, password)
        if syncer.connect():
            syncer.run_comprehensive_sync(days_back=30, activity_limit=50)