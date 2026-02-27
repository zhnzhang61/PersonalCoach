import sys
import subprocess
import os
import importlib.util
import json  # <--- Added for permanent lap memory

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
        'tabulate': 'tabulate',
        'altair': 'altair',
        'langgraph': 'langgraph',
        'langchain_google_genai': 'langchain-google-genai',
        'langchain_core': 'langchain-core',
        'langgraph.checkpoint.sqlite': 'langgraph-checkpoint-sqlite'
    }
    for import_name, pip_name in packages.items():
        try:
            if importlib.util.find_spec(import_name) is None:
                install_package(pip_name)
        except ModuleNotFoundError:
            install_package(pip_name)

check_dependencies()

if __name__ == "__main__":
    if "streamlit" not in sys.modules:
        cmd = [sys.executable, "-m", "streamlit", "run", __file__]
        subprocess.run(cmd)
        sys.exit()

import streamlit as st
import pandas as pd
import datetime
import altair as alt
from data_processor import DataProcessor
from agentic_coach import AgenticCoach 

st.set_page_config(layout="wide", page_title="Training Block Manager")

# Initialize Processors
processor = DataProcessor()

# Initialize the LangGraph Agent globally
if "agent" not in st.session_state:
    st.session_state.agent = AgenticCoach()
    st.session_state.thread_id = "unified_copilot_thread" 

agent = st.session_state.agent
thread_id = st.session_state.thread_id

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
    blocks = processor.get_blocks()
    if not blocks: st.stop()
    current_block = blocks[0] 
    weeks = processor.get_weeks_for_block(current_block['id'])
    week_opts = {w['label']: w for w in weeks}
    selected_week_label = st.selectbox("Select Week", list(week_opts.keys()), index=0)
    current_week = week_opts[selected_week_label]

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
                                    report = agent.analyze_run(ctx, thread_id=thread_id)
                                    st.session_state[f"report_{run_id}"] = report
                                    st.rerun()

                if f"report_{run_id}" in st.session_state:
                    st.markdown("---")
                    st.markdown(f"### 📋 Coach's Review")
                    st.markdown(st.session_state[f"report_{run_id}"])
                    if st.button("Close Report", key=f"close_{run_id}"):
                        del st.session_state[f"report_{run_id}"]
                        st.rerun()

                # =========================================
                # NEW DYNAMIC LAP EDITOR (NO FORM)
                # =========================================
                if st.session_state.get('editing_run_id') == run_id:
                    st.divider()
                    st.markdown(f"#### ✏️ Categorize Laps")
                    
                    # 1. State Management & Permanent Disk Storage
                    lap_save_path = os.path.join("data", "get_activity_splits", f"{run_id}_cats.json")
                    state_key = f"lap_cats_{run_id}"
                    
                    laps = processor.get_run_laps(run_id)
                    
                    if laps:
                        # Load previous saved categories if they exist, else default
                        if state_key not in st.session_state:
                            if os.path.exists(lap_save_path):
                                with open(lap_save_path, 'r') as f:
                                    st.session_state[state_key] = json.load(f)
                            else:
                                st.session_state[state_key] = ["Hold Back Easy"] * len(laps)
                                
                        new_name = st.text_input("Run Name", value=meta.get('name', run.get('activityName', '')))
                        w_num = st.number_input("Week #", value=meta.get('week_num', current_week['week_num']))
                        
                        lap_rows = []
                        for i, lap in enumerate(laps):
                            d_mi = lap.get('distance', 0) / 1609.34
                            t_sec = lap.get('duration', 0)
                            pace = "N/A"
                            if t_sec > 0 and d_mi > 0:
                                p_min = (t_sec / 60) / d_mi
                                pace = f"{int(p_min)}:{int((p_min % 1) * 60):02d}"
                            
                            # Safely fetch state
                            cat = st.session_state[state_key][i] if i < len(st.session_state[state_key]) else "Hold Back Easy"
                            
                            lap_rows.append({
                                "Lap": i + 1,
                                "Dist (mi)": round(d_mi, 2),
                                "Pace": pace,
                                "Avg HR": lap.get('averageHR', 0),
                                "category": cat 
                            })
                        
                        cat_options = ["Hold Back Easy", "Steady Effort", "Increasing Effort", "Marathon", "LT Effort", "VO2Max", "Sprint", "Rest"]
                        
                        # 2. Batch Edit UI
                        st.markdown("##### ⚡️ Batch Edit Laps")
                        col_batch1, col_batch2, col_batch3 = st.columns([2, 2, 1])
                        with col_batch1:
                            batch_laps = st.multiselect("Select Laps to update:", options=[r["Lap"] for r in lap_rows], key=f"bl_{run_id}")
                        with col_batch2:
                            batch_cat = st.selectbox("Assign Category:", options=cat_options, key=f"bc_{run_id}")
                        with col_batch3:
                            st.write("") # Formatting padding
                            st.write("")
                            if st.button("Apply to Selected", key=f"apply_{run_id}"):
                                for lap_num in batch_laps:
                                    idx = lap_num - 1
                                    st.session_state[state_key][idx] = batch_cat
                                st.rerun() # Immediately visually updates the grid
                                
                        # 3. Individual Laps Grid
                        st.markdown("##### 📝 Individual Laps")
                        edited_laps = st.data_editor(
                            lap_rows,
                            column_config={
                                "category": st.column_config.SelectboxColumn("Category", options=cat_options, required=True)
                            },
                            hide_index=True,
                            key=f"editor_{run_id}"
                        )
                        
                        # Sync any manual cell clicks instantly back to session state
                        for i, row in enumerate(edited_laps):
                            if i < len(st.session_state[state_key]):
                                st.session_state[state_key][i] = row['category']
                                
                        st.markdown("<br>", unsafe_allow_html=True)
                        
                        # 4. Save Actions
                        c1, c2, c3 = st.columns([1, 1, 4])
                        with c1:
                            if st.button("Save & Calculate", type="primary", key=f"save_{run_id}"):
                                # Apply final categories to the underlying lap objects
                                for i, row in enumerate(edited_laps):
                                    laps[i]['category'] = row['category']
                                
                                # PERMANENTLY save lap assignments to disk
                                with open(lap_save_path, 'w') as f:
                                    json.dump(st.session_state[state_key], f)
                                    
                                cat_stats = processor.calculate_category_stats(laps)
                                processor.save_run_metadata(run_id, w_num, new_name, cat_stats)
                                st.session_state['editing_run_id'] = None
                                st.rerun()
                        with c2:
                            if st.button("Cancel", key=f"cancel_{run_id}"):
                                st.session_state['editing_run_id'] = None
                                st.rerun()
                    else:
                        st.error("No valid laps found.")
                        if st.button("Cancel", key=f"cancel_err_{run_id}"):
                            st.session_state['editing_run_id'] = None
                            st.rerun()

# ==========================================
# TAB 2: RECOVERY & HEALTH
# ==========================================
with tab_health:
    st.header("Holistic Health View")
    
    stats = processor.get_health_stats()
    if not stats:
        st.warning("No health data found. Please run sync.")
    else:
        df = pd.DataFrame(stats)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)

        col1, col2, col3, col4 = st.columns(4)
        last_day = df.iloc[-1]
        
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

        st.divider()

        st.subheader("HRV Status (7-Day Average vs Baseline)")
        df['hrv_7d'] = df['hrv'].rolling(window=7, min_periods=1).mean()
        df['baseline_mean'] = df['hrv'].rolling(window=21, min_periods=1).mean()
        df['baseline_std'] = df['hrv'].rolling(window=21, min_periods=1).std().clip(lower=3.5)
        df['baseline_high'] = df['baseline_mean'] + df['baseline_std']
        df['baseline_low'] = df['baseline_mean'] - df['baseline_std']

        def determine_hrv_status(row):
            if pd.isna(row['hrv_7d']) or pd.isna(row['baseline_low']): 
                return "Unknown"
            if row['hrv_7d'] < row['baseline_low'] or row['hrv_7d'] > row['baseline_high']:
                return "Unbalanced"
            return "Balanced"

        df['hrv_status'] = df.apply(determine_hrv_status, axis=1)

        chart_df = df.reset_index().dropna(subset=['hrv_7d'])

        baseline_band = alt.Chart(chart_df).mark_area(opacity=0.15, color='#888888').encode(
            x=alt.X('date:T', title='Date'),
            y=alt.Y('baseline_low:Q', title='HRV (ms)', scale=alt.Scale(zero=False)),
            y2='baseline_high:Q'
        )

        hrv_line = alt.Chart(chart_df).mark_line(color='#A0A0A0', size=1.5).encode(
            x='date:T',
            y='hrv_7d:Q'
        )

        hrv_points = alt.Chart(chart_df).mark_circle(size=80).encode(
            x='date:T',
            y='hrv_7d:Q',
            color=alt.Color('hrv_status:N', 
                            scale=alt.Scale(domain=['Balanced', 'Unbalanced', 'Unknown'], 
                                            range=['#2ca02c', '#d62728', '#7f7f7f']),
                            legend=alt.Legend(title="Status")),
            tooltip=[
                alt.Tooltip('date:T', title='Date'),
                alt.Tooltip('hrv_7d:Q', title='7-Day Avg HRV', format='.1f'),
                alt.Tooltip('hrv:Q', title='Last Night HRV', format='.1f'),
                alt.Tooltip('baseline_low:Q', title='Baseline Low', format='.1f'),
                alt.Tooltip('baseline_high:Q', title='Baseline High', format='.1f'),
                alt.Tooltip('hrv_status:N', title='Status')
            ]
        )

        final_hrv_chart = alt.layer(baseline_band, hrv_line, hrv_points).properties(height=300).interactive()
        st.altair_chart(final_hrv_chart, use_container_width=True)

        col_charts_1, col_charts_2 = st.columns(2)
        with col_charts_1:
            st.subheader("Sleep Quality vs. Volume")
            st.line_chart(df[['sleep_score', 'run_miles']])
        with col_charts_2:
            st.subheader("RHR vs. Stress")
            st.line_chart(df[['rhr', 'stress']])

        # ==========================================
        # 4. LANGGRAPH AGENT CHATBOX
        # ==========================================
        st.divider()
        st.subheader("🤖 Unified AI Co-Pilot")
        st.caption("Ask questions about your pacing, HRV, sleep, or training blocks. The Supervisor will route it to the right expert.")

        if st.button("🩺 Analyze Today's Health"):
            with st.spinner("Doctor is reviewing your charts..."):
                yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
                raw_sleep = processor.load_json_safe(processor.paths['sleep'], f"{yesterday_str}.json")
                agent.analyze_health(
                    history_df=df.tail(14), 
                    yesterday_raw=raw_sleep, 
                    thread_id=thread_id
                )
                st.rerun()

        if prompt := st.chat_input("Ask your agents a question..."):
            with st.spinner("Supervisor is routing your request..."):
                today_str = datetime.date.today().isoformat()
                context = f"""
                INTERNAL DATA FACT SHEET:
                Today's Date is: {today_str}
                Today's HRV: {last_day.get('hrv', 'unknown')} ms. 
                Sleep Score: {last_day.get('sleep_score', 'unknown')}. 
                Last 7 days run miles: {df.tail(7)['run_miles'].sum():.1f}.
                """
                agent.chat(
                    user_input=prompt, 
                    thread_id=thread_id, 
                    system_context=context
                )
            st.rerun()

        history = agent.get_history(thread_id)
        chat_msgs = [msg for msg in history if msg.type != "system"]

        interactions = []
        current_interaction = []
        
        for msg in chat_msgs:
            if msg.type == "human":
                if current_interaction:
                    interactions.append(current_interaction)
                current_interaction = [msg]
            else:
                current_interaction.append(msg)
                
        if current_interaction:
            interactions.append(current_interaction)

        recent_interactions = interactions[-10:][::-1]

        for interaction in recent_interactions:
            for msg in interaction:
                content = msg.content
                if isinstance(content, list):
                    content = "".join([block.get("text", "") for block in content if isinstance(block, dict) and "text" in block])
                    
                role = "user" if msg.type == "human" else "assistant"
                with st.chat_message(role):
                    st.markdown(content)