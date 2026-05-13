import os
import time
import json
import inspect
import datetime
import sys
import subprocess
from dotenv import load_dotenv

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

    def connect(self, no_fallback: bool = False):
        token_dir = os.path.join(os.path.expanduser("~"), ".garth")
        try:
            # 1. 尝试直接用现有的通行证免密登录
            print("Trying to login using cached token...")
            client = Garmin()
            client.login(token_dir)
            self.client = client
            print("✅ Login successful using cached token!")

            self._introspect_api()
            return True

        except Exception:
            # 2. 如果现存 Token 失败或过期
            print("\n⚠️ Token expired or not found.", flush=True)

            if no_fallback:
                # API / launchd 模式：不能弹浏览器、不能等用户输入
                print("TOKEN_EXPIRED", file=sys.stderr, flush=True)
                return False

            # CLI 交互模式：触发手动浏览器登录打断点
            print("Initiating manual browser login fallback...", flush=True)

            # 动态获取 garmin_ticket_login.py 的路径 (确保它和当前脚本在同一个文件夹)
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "garmin_ticket_login.py")

            if not os.path.exists(script_path):
                print(f"❌ Error: Could not find {script_path}.")
                print("Fallback failed. Please run the login script manually.")
                return False

            try:
                # 调用脚本：打开浏览器并生成兼容的 oauth1 和 domain_profile 占位文件
                result = subprocess.run([sys.executable, script_path, "--open-browser", "--compat"])

                # 检查子脚本是否顺利执行完毕（返回 0 代表成功）
                if result.returncode != 0:
                    print("❌ Manual login process was aborted or failed.")
                    return False

                # 3. 手动登录成功，Token 已经写好，重新尝试加载免密登录
                print("\n🔄 Loading newly fetched token...")
                client = Garmin()
                client.login(token_dir)
                self.client = client
                print("✅ Login successful using new token!")

                self._introspect_api()
                return True

            except Exception as e:
                print(f"❌ Failed to process manual login: {e}")
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
        # Whether to (re-)fetch is decided by the caller; once data is in
        # hand, always write it. The legacy "skip if file exists" guard here
        # silently dropped re-syncs of today's sleep/HRV when an earlier sync
        # had landed an empty husk before Garmin's cloud caught up.
        if not data: return
        folder_path = os.path.join(self.data_dir, method_name)
        os.makedirs(folder_path, exist_ok=True)
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
        # Today's and yesterday's data is fluid — the watch can take hours to
        # finish uploading last night's sleep, HRV, readiness etc. Always
        # re-fetch those two days so we pick up data that wasn't ready when
        # an earlier sync ran. Files older than that are stable; existence
        # check is fine and saves API calls.
        print(f"⬇️ Syncing Daily Data ({days_back} days)...")
        today_iso = datetime.date.today().isoformat()
        yesterday_iso = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        always_refetch = {today_iso, yesterday_iso}
        for i in range(days_back):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            for method, arg_name in self.daily_methods:
                try:
                    file_path = os.path.join(self.data_dir, method, f"{d}.json")
                    if d in always_refetch or not os.path.exists(file_path):
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
    no_fallback = "--no-fallback" in sys.argv
    # Email / Pass 可以留着做备用变量，虽然新逻辑下用不到了
    syncer = GarminSync(email, password)
    if syncer.connect(no_fallback=no_fallback):
        syncer.run_sync(days_back=5, activity_limit=5)
    else:
        # connect() 已经把原因写到 stderr，用 exit code 区分:
        # 2 = token 过期/无效, 1 = 其他失败
        sys.exit(2 if no_fallback else 1)