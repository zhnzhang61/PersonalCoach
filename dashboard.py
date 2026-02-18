import sys
import subprocess
import os
import importlib.util

def install_package(package_name):
    print(f"📦 Installing missing package: {package_name}...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', package_name])

def check_dependencies():
    packages = {
        'streamlit': 'streamlit',
        'pandas': 'pandas',
        'google.genai': 'google-genai', 
        'garminconnect': 'garminconnect',
        'dotenv': 'python-dotenv',
        'tabulate': 'tabulate'
    }
    for import_name, pip_name in packages.items():
        if importlib.util.find_spec(import_name) is None:
            install_package(pip_name)

if __name__ == "__main__":
    if "streamlit" not in sys.modules:
        check_dependencies()
        cmd = [sys.executable, "-m", "streamlit", "run", __file__]
        subprocess.run(cmd)
        sys.exit()

import streamlit as st
import pandas as pd
import datetime
from data_processor import DataProcessor
from ai_analyst import AIAnalyst

st.set_page_config(layout="wide", page_title="Training Block Manager")

# Initialize Processors
processor = DataProcessor()
analyst = AIAnalyst(model="gemini-flash-latest")

st.title("🏃‍♂️ Training & Health Dashboard")

# --- GLOBAL SIDEBAR ACTIONS ---
if st.sidebar.button("🔄 Sync Health Data"):
    with st.spinner("Aggregating daily metrics..."):
        processor.compile_health_ledger()
    st.sidebar.success("Updated!")

# --- TABS ---
tab_train, tab_health = st.tabs(["🏋️ Training Log", "❤️ Recovery & Health"])

# ==========================================
# TAB 1: EXISTING TRAINING LOGIC
# ==========================================
with tab_train:
    # Block & Week Selection
    blocks = processor.get_blocks()
    if not blocks: st.stop()
    current_block = blocks[0] 
    weeks = processor.get_weeks_for_block(current_block['id'])
    week_opts = {w['label']: w for w in weeks}
    selected_week_label = st.selectbox("Select Week", list(week_opts.keys()), index=0)
    current_week = week_opts[selected_week_label]

    # Layout
    col_main, col_sidebar = st.columns([3, 1])

    with col_sidebar:
        st.subheader("Auxiliary Log")
        with st.expander("➕ Log Activity"):
            with st.form("aux_form"):
                a_date = st.date_input("Date")
                a_type = st.text_input("Type")
                a_desc = st.text_area("Description")
                if st.form_submit_button("Save"):
                    processor.add_aux_activity(a_date.isoformat(), a_type, a_desc)
                    st.rerun()
        
        st.divider()
        auxs = processor.get_aux_in_range(current_week['start'], current_week['end'])
        for a in auxs:
            st.info(f"**{a['date']}** | {a['type']}\n\n{a['desc']}")

    with col_main:
        runs = processor.get_activities_in_range(current_week['start'], current_week['end'])
        runs_only = [r for r in runs if 'running' in r.get('activityType', {}).get('typeKey', '')]
        
        total_dist = sum([r.get('distance', 0) for r in runs_only]) / 1609.34
        st.info(f"**Week Stats:** {len(runs_only)} Runs | {total_dist:.1f} Miles")
        
        for run in runs_only:
            run_id = run['activityId']
            meta = run.get('manual_meta', {})
            has_stats = 'category_stats' in meta
            
            splits_path = os.path.join("data", "get_activity_splits", f"{run_id}.json")
            has_splits = os.path.exists(splits_path)

            with st.container(border=True):
                # Top Row: Stats & Buttons
                c1, c2 = st.columns([4, 1])
                with c1:
                    display_name = meta.get('name', run.get('activityName', 'Run'))
                    st.subheader(display_name)
                    dist_mi = run.get('distance', 0) / 1609.34
                    st.caption(f"{run.get('startTimeLocal', '')[:10]} | {dist_mi:.2f} mi")
                    
                    if has_stats:
                        df = pd.DataFrame(meta['category_stats'])
                        st.dataframe(df, hide_index=True)
                    elif not has_splits:
                         st.warning("⚠️ Splits not synced.")
                    else:
                        st.info("ℹ️ Uncategorized. Click Edit.")
                
                with c2:
                    if has_splits:
                        if st.button("Edit", key=f"ed_{run_id}"):
                            st.session_state['editing_run_id'] = run_id
                            st.rerun()
                    
                    if has_stats:
                        if st.button("Analyze", key=f"ai_{run_id}"):
                            with st.spinner("Coach is thinking..."):
                                ctx = processor.build_ai_context(run_id, current_block['id'])
                                if ctx:
                                    report = analyst.analyze_run(ctx)
                                    st.session_state[f"report_{run_id}"] = report
                                    st.rerun()

                # --- COACH'S REVIEW ---
                if f"report_{run_id}" in st.session_state:
                    st.markdown("---")
                    st.markdown(f"### 📋 Coach's Review")
                    st.markdown(st.session_state[f"report_{run_id}"])
                    if st.button("Close Report", key=f"close_{run_id}"):
                        del st.session_state[f"report_{run_id}"]
                        st.rerun()

                # --- INLINE EDITOR ---
                if st.session_state.get('editing_run_id') == run_id:
                    st.divider()
                    st.markdown(f"#### ✏️ Categorize Laps")
                    
                    with st.form(key=f"edit_form_{run_id}"):
                        new_name = st.text_input("Run Name", value=meta.get('name', run.get('activityName', '')))
                        w_num = st.number_input("Week #", value=meta.get('week_num', current_week['week_num']))
                        
                        laps = processor.get_run_laps(run_id)
                        
                        if laps:
                            lap_rows = []
                            for i, lap in enumerate(laps):
                                d_mi = lap.get('distance', 0) / 1609.34
                                t_sec = lap.get('duration', 0)
                                pace = "N/A"
                                if t_sec > 0 and d_mi > 0:
                                    p_min = (t_sec / 60) / d_mi
                                    pace = f"{int(p_min)}:{int((p_min % 1) * 60):02d}"
                                
                                lap_rows.append({
                                    "Lap": i + 1,
                                    "Dist (mi)": round(d_mi, 2),
                                    "Pace": pace,
                                    "Avg HR": lap.get('averageHR', 0),
                                    "category": "Hold Back Easy" 
                                })
                            
                            cat_options = ["Hold Back Easy", "Steady Effort", "Increasing Effort", "Marathon", "LT Effort", "VO2Max", "Sprint", "Rest"]
                            
                            edited_laps = st.data_editor(
                                lap_rows,
                                column_config={
                                    "category": st.column_config.SelectboxColumn("Category", options=cat_options, required=True)
                                },
                                hide_index=True,
                                key=f"editor_{run_id}"
                            )

                            if st.form_submit_button("Save & Calculate"):
                                for i, row in enumerate(edited_laps):
                                    laps[i]['category'] = row['category']
                                
                                cat_stats = processor.calculate_category_stats(laps)
                                processor.save_run_metadata(run_id, w_num, new_name, cat_stats)
                                st.session_state['editing_run_id'] = None
                                st.rerun()
                        else:
                            st.error("No valid laps found.")
                            if st.form_submit_button("Cancel"):
                                st.session_state['editing_run_id'] = None
                                st.rerun()

# ==========================================
# TAB 2: RECOVERY & HEALTH
# ==========================================
with tab_health:
    st.header("Holistic Health View")
    
    # 1. Load Data
    stats = processor.get_health_stats()
    if not stats:
        st.warning("No health data found. Please run sync.")
    else:
        df = pd.DataFrame(stats)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)

        # 2. Key Metrics Row
        col1, col2, col3, col4 = st.columns(4)
        last_day = df.iloc[-1]
        
        # Helper for safer delta calculation
        def safe_metric(label, key, avg_key=None, inverse=False):
            val = last_day.get(key)
            if pd.isna(val): return st.metric(label, "N/A")
            
            val = float(val)
            delta = None
            if avg_key and not pd.isna(df[avg_key].mean()):
                diff = val - df[avg_key].mean()
                delta = f"{diff:.1f}"
                
            color = "inverse" if inverse else "normal"
            st.metric(label, f"{int(val)}", delta=delta, delta_color=color)

        with col1: safe_metric("Sleep Score", "sleep_score", "sleep_score")
        with col2: safe_metric("RHR", "rhr", "rhr", inverse=True)
        with col3: safe_metric("HRV (ms)", "hrv", "hrv")
        with col4: safe_metric("Stress", "stress", "stress", inverse=True)

        # 3. Charts
        st.subheader("Trends: Sleep Quality vs. Training Volume")
        st.line_chart(df[['sleep_score', 'run_miles']])
        
        st.subheader("Physiological Load: RHR vs. Stress")
        st.line_chart(df[['rhr', 'stress']])

        # 4. AI Holistic Analysis
        st.divider()
        st.subheader("🤖 Dr. AI Health Check")
        if st.button("Analyze Recent Health Trends"):
            with st.spinner("Reading sleep files and analyzing trends..."):
                # Fetch yesterday's raw sleep file for deep dive
                yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
                raw_sleep = processor.load_json_safe(processor.paths['sleep'], f"{yesterday_str}.json")
                
                analysis = analyst.analyze_holistic_health(
                    history_df=df.tail(14), # Last 14 days context
                    yesterday_raw=raw_sleep
                )
                st.markdown(analysis)