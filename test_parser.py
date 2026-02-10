import os
import fitdecode
import sys

# 1. Setup Paths
DATA_DIR = 'data'
ACT_DIR = os.path.join(DATA_DIR, 'activities')

print(f"📂 Checking {ACT_DIR}...")

# 2. Find the most recent .fit file
try:
    files = [f for f in os.listdir(ACT_DIR) if f.endswith('.fit')]
except FileNotFoundError:
    print(f"❌ Error: Directory {ACT_DIR} not found.")
    sys.exit()

if not files:
    print("❌ Error: No .fit files found in data/activities.")
    sys.exit()

# Get the newest file to test (likely the one you are trying to edit)
latest_file = max(files, key=lambda x: os.path.getmtime(os.path.join(ACT_DIR, x)))
fit_path = os.path.join(ACT_DIR, latest_file)
print(f"🧐 Inspecting newest file: {latest_file}")

# 3. Test Parsing Logic
print("-" * 40)
found_fields = set()
record_count = 0

try:
    with fitdecode.FitReader(fit_path) as fit:
        for frame in fit:
            if frame.frame_type == fitdecode.FIT_FRAME_DATA and frame.name == 'record':
                record_count += 1
                # Log available fields in the first record only
                if record_count == 1:
                    print("✅ Found 'record' messages.")
                    print("   Available Fields in first record:")
                    for field in frame.fields:
                        print(f"   - {field.name} (Value: {field.value}, Units: {field.units})")
                        found_fields.add(field.name)
                
                # Check for specific fields we depend on
                if 'heart_rate' in frame.fields: found_fields.add('heart_rate')
                if 'enhanced_speed' in frame.fields: found_fields.add('enhanced_speed')
                if 'speed' in frame.fields: found_fields.add('speed')
                if 'distance' in frame.fields: found_fields.add('distance')

except Exception as e:
    print(f"❌ CRITICAL ERROR reading .fit file: {e}")
    sys.exit()

print("-" * 40)
print(f"📊 Summary for {latest_file}:")
print(f"   Total Data Records: {record_count}")

# 4. Validate Assumptions
missing = []
if 'distance' not in found_fields: missing.append('distance')
if 'heart_rate' not in found_fields: missing.append('heart_rate')
if 'enhanced_speed' not in found_fields and 'speed' not in found_fields: missing.append('speed/enhanced_speed')

if missing:
    print(f"❌ MISSING DATA: The file lacks these fields: {missing}")
    print("   -> This explains why the calculation returns None.")
else:
    print("✅ All required fields (distance, heart_rate, speed) exist.")
    print("   -> If the app crashes, the issue is likely the specific TIME RANGE you selected.")
    print("   -> (e.g., You asked for Mile 5-6, but the file only goes to Mile 4).")