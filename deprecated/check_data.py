# check_data.py
import os, json

print(f"{'DATE':<12} | {'TYPE':<15} | {'NAME'}")
print("-" * 50)

folder = 'data/activities'
files = [f for f in os.listdir(folder) if f.endswith('_summary.json')]

# Sort by filename (which usually correlates to ID) or just parse all
activities = []
for f in files:
    try:
        data = json.load(open(os.path.join(folder, f)))
        activities.append(data)
    except: pass

# Sort by Date Newest -> Oldest
activities.sort(key=lambda x: x['startTimeLocal'], reverse=True)

for act in activities:
    date = act.get('startTimeLocal', 'Unknown')[:10]
    kind = act['activityType']['typeKey']
    name = act['activityName']
    print(f"{date} | {kind:<15} | {name}")