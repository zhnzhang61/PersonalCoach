import sys
import subprocess
import os
import importlib.util

def install_package(package_name):
    print(f"📦 Installing missing package: {package_name}...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', package_name])

# def check_dependencies():
#     packages = {
#         'streamlit': 'streamlit',
#         'pandas': 'pandas',
#         'google.genai': 'google-genai', 
#         'garminconnect': 'garminconnect',
#         'dotenv': 'python-dotenv',
#         'tabulate': 'tabulate',
#         'altair': 'altair',
#         'langgraph': 'langgraph',
#         'langchain_google_genai': 'langchain-google-genai',
#         'langchain_core': 'langchain-core',
#         'langgraph.checkpoint.sqlite': 'langgraph-checkpoint-sqlite'
#     }
#     for import_name, pip_name in packages.items():
#         try:
#             if importlib.util.find_spec(import_name) is None:
#                 install_package(pip_name)
#         except ModuleNotFoundError:
#             install_package(pip_name)

# check_dependencies()

if __name__ == "__main__":
    if "streamlit" not in sys.modules:
        cmd = [sys.executable, "-m", "streamlit", "run", __file__]
        subprocess.run(cmd)
        sys.exit()

import streamlit as st
import pandas as pd
import datetime
import json
import altair as alt
from data_processor import DataProcessor
from agentic_coach import AgenticCoach
from cognitive_memory_engine import MemoryOS

st.set_page_config(layout="wide", page_title="Training Block Manager")


def _try_resolve_pending(user_text: str, memory_engine):
    """
    Check if the user's message is answering any pending clarification.
    Uses simple keyword matching against pending questions.
    Resolves all unresolved pending items with the user's answer.
    """
    pending = memory_engine.list_pending(resolved=False)
    if not pending:
        return
    for p in pending:
        # If user mentions the pending ID explicitly, or if there's only one pending item
        if p["pending_id"] in user_text or len(pending) == 1:
            memory_engine.resolve_pending_question(p["pending_id"], user_text)
            st.toast(f"✅ 已记录你的回答并更新记忆: {p['pending_id']}")
            return

# Initialize Processors
processor = DataProcessor()

# Initialize the LangGraph Agent globally (with Cognitive Memory Engine)
if "agent" not in st.session_state:
    memory_engine = MemoryOS(
        db_path="data/cognition.db",
        semantic_profile_path=processor.paths["semantic_memory"],
    )
    st.session_state.memory_engine = memory_engine
    st.session_state.agent = AgenticCoach(memory_engine=memory_engine)
    st.session_state.thread_id = "unified_copilot_thread"

agent = st.session_state.agent
thread_id = st.session_state.thread_id

st.title("🏃‍♂️ Training & Health Dashboard")

# --- GLOBAL SIDEBAR ACTIONS ---
# --- GLOBAL SIDEBAR ACTIONS ---
st.sidebar.subheader("🔄 Data Management")

if st.sidebar.button("☁️ Download Garmin Data"):
    with st.spinner("Syncing with Garmin Connect... (This may take a minute)"):
        try:
            # Run the sync script and capture the output
            result = subprocess.run([sys.executable, "garmin_sync.py"], capture_output=True, text=True)
            
            if result.returncode == 0:
                st.sidebar.success("Garmin data downloaded successfully!")
            else:
                st.sidebar.error("Garmin sync failed.")
                with st.sidebar.expander("View Error Log"):
                    st.code(result.stderr or result.stdout)
        except Exception as e:
            st.sidebar.error(f"Execution error: {e}")

if st.sidebar.button("📊 Update Health Ledger"):
    with st.spinner("Aggregating daily metrics..."):
        processor.compile_health_ledger()
    st.sidebar.success("Health ledger updated!")

st.sidebar.divider()

st.sidebar.divider()
st.sidebar.subheader("⚙️ AI Telemetry Settings")
downsample_sec = st.sidebar.slider("Sampling Interval (sec)", min_value=5, max_value=60, value=10, step=5, help="Controls how granular the data sent to the AI is. Lower = More detail but more tokens.")

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
    
    # --- 新增: 计算当前周的索引 ---
    today_iso = datetime.date.today().isoformat()
    default_idx = 0
    for i, w in enumerate(weeks):
        if w['start'] <= today_iso <= w['end']:
            default_idx = i
            break
        elif today_iso > w['end']:
            default_idx = i # 如果今天已经超过了课表，默认停留在最后一周

    # 把 default_idx 传给 selectbox
    selected_week_label = st.selectbox("Select Week", list(week_opts.keys()), index=default_idx)
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
            telemetry_path = os.path.join("data", "get_activity_details", f"{run_id}.json")
            has_telemetry = os.path.exists(telemetry_path)

            laps = processor.get_run_laps(run_id)
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
                            # 检查是否已有历史对话，有则直接加载，无则调用 API
                            existing_chat = processor.get_run_chat_history(run_id)
                            if existing_chat:
                                # 从历史记录中恢复：第一条 assistant 消息即为原始报告
                                first_report = next(
                                    (m["content"] for m in existing_chat if m["role"] == "assistant"),
                                    None
                                )
                                if first_report:
                                    st.session_state[f"report_{run_id}"] = first_report
                                    st.rerun()

                            # 没有历史对话，首次生成分析
                            with st.spinner("Coach is thinking..."):
                                ctx = processor.build_agent_working_memory(run_id, current_block['id'])

                                if "error" not in ctx:
                                    ctx['workout_summary']['name'] = meta.get('name', run.get('activityName', 'Unnamed Workout'))
                                    ctx['workout_summary']['notes'] = meta.get('notes', '')

                                    df_ai = None
                                    if has_telemetry and hasattr(processor, 'get_activity_telemetry'):
                                        _, df_ai = processor.get_activity_telemetry(run_id, laps=laps, downsample_sec=downsample_sec)

                                    history = processor.search_episodic_memories(limit=3)

                                    report = agent.analyze_run(
                                        working_memory_dict=ctx,
                                        thread_id=f"run_analysis_{run_id}",
                                        telemetry_df=df_ai,
                                        historical_memories=history
                                    )

                                    st.session_state[f"report_{run_id}"] = report

                                    # 将首次分析报告也存入本地对话历史
                                    processor.save_run_chat_message(run_id, "assistant", report)

                                    # 生成 Episodic Memory
                                    try:
                                        memory_payload = agent.generate_episodic_summary(ctx, telemetry_df=df_ai)
                                        processor.save_episodic_memory(
                                            activity_id=run_id,
                                            date=ctx['date'],
                                            summary_text=memory_payload.get('summary_text', 'Run completed.'),
                                            tags=memory_payload.get('tags', [])
                                        )
                                    except Exception as e:
                                        st.sidebar.warning(f"Failed to generate episodic memory for {run_id}: {e}")

                                    st.rerun()
                                else:
                                    st.error("Could not build agent context.")

                # --- TELEMETRY VIEWER ---
                if has_telemetry and hasattr(processor, 'get_activity_telemetry'):
                    with st.expander("📈 View Telemetry Curves"):
                        # --- UPDATE THIS ---
                        df_raw, df_ai = processor.get_activity_telemetry(run_id, laps=laps, downsample_sec=downsample_sec)
                        if df_raw is not None and not df_raw.empty:
                            tab_hr, tab_pace, tab_elev, tab_ai_view = st.tabs(["❤️ Heart Rate", "👟 Pace", "⛰️ Elevation", "🤖 AI Data View"])
                            
                            with tab_hr:
                                hr_chart = alt.Chart(df_raw.dropna(subset=['HeartRate'])).mark_line(color='#ff4b4b').encode(
                                    x=alt.X('Second:Q', title='Time (seconds)'),
                                    y=alt.Y('HeartRate:Q', scale=alt.Scale(zero=False), title='Heart Rate (bpm)'),
                                    tooltip=['Second', 'HeartRate']
                                ).interactive()
                                st.altair_chart(hr_chart, use_container_width=True)
                                
                            with tab_pace:
                                # Inverse Y axis for pace (lower is faster)
                                pace_df = df_raw.dropna(subset=['Pace']).copy()
                                pace_df['Pace'] = pace_df['Pace'].clip(lower=4, upper=15)
                                pace_chart = alt.Chart(pace_df).mark_line(color='#4b4bff').encode(
                                    x=alt.X('Second:Q', title='Time (seconds)'),
                                    y=alt.Y('Pace:Q', scale=alt.Scale(reverse=True), title='Pace (min/mi)'),
                                    tooltip=['Second', 'Pace']
                                ).interactive()
                                st.altair_chart(pace_chart, use_container_width=True)
                            
                            with tab_elev:
                                elev_df = df_raw.dropna(subset=['Elevation']).copy()
                                
                                # Use mark_area to make it look like a solid hill/terrain profile
                                elev_area = alt.Chart(elev_df).mark_area(opacity=0.3, color='#2ca02c').encode(
                                    x=alt.X('Second:Q', title='Time (seconds)'),
                                    y=alt.Y('Elevation:Q', scale=alt.Scale(zero=False), title='Elevation'),
                                )
                                elev_line = alt.Chart(elev_df).mark_line(color='#2ca02c', size=2).encode(
                                    x=alt.X('Second:Q', title='Time (seconds)'),
                                    y=alt.Y('Elevation:Q', scale=alt.Scale(zero=False), title='Elevation'),
                                    tooltip=['Second', 'Elevation']
                                )
                                
                                st.altair_chart((elev_area + elev_line).interactive(), use_container_width=True)

                            with tab_ai_view:
                                st.caption(f"This is the heavily downsampled CSV ({downsample_sec}s intervals) sent to the AI Coach.")
                                st.dataframe(df_ai, hide_index=True)

                if f"report_{run_id}" in st.session_state:
                    st.markdown("---")
                    st.markdown(f"### 📋 Coach's Review")
                    st.markdown(st.session_state[f"report_{run_id}"])
                    
                    # --- NEW: FOLLOW-UP CHAT UI ---
                    st.markdown("#### 💬 Discuss this Run")
                    
                    # 1. 渲染历史对话记录
                    chat_history = processor.get_run_chat_history(run_id)
                    for msg in chat_history:
                        with st.chat_message(msg["role"]):
                            st.markdown(msg["content"])
                            
                    # 2. 渲染输入框并处理新问题
                    if run_prompt := st.chat_input("Ask a follow-up about this run...", key=f"chat_input_{run_id}"):
                        # 尝试自动解析待确认问题的回答
                        if 'memory_engine' in st.session_state:
                            _try_resolve_pending(run_prompt, st.session_state.memory_engine)

                        # 立即存入本地并展示用户的提问
                        processor.save_run_chat_message(run_id, "user", run_prompt)
                        with st.chat_message("user"):
                            st.markdown(run_prompt)

                        # 呼叫 AI 进行回答
                        with st.chat_message("assistant"):
                            with st.spinner("Coach is reviewing the telemetry..."):
                                # 发送到这个 run_id 专属的 Thread 中
                                response = agent.follow_up_chat(user_input=run_prompt, thread_id=f"run_analysis_{run_id}")
                                st.markdown(response)
                                # 将 AI 的回答存入本地
                                processor.save_run_chat_message(run_id, "assistant", response)

                        st.rerun() # 刷新 UI 状态

                    st.markdown("<br>", unsafe_allow_html=True)
                    if st.button("Close Report", key=f"close_{run_id}"):
                        with st.spinner("🧠 正在巩固记忆档案..."):
                            # 1. 总结这段对话的精华
                            chat_summary = agent.summarize_thread(f"run_analysis_{run_id}")
                            if chat_summary:
                                # 2. 将精华追加到情景记忆库中
                                processor.append_chat_to_episodic_memory(run_id, chat_summary)
                            # 3. CME: 提取话题、事件、冲突到认知图谱
                            agent.consolidate_and_learn(f"run_analysis_{run_id}")

                        del st.session_state[f"report_{run_id}"]
                        st.rerun()

                # =========================================
                # NEW DYNAMIC LAP EDITOR
                # =========================================
                if st.session_state.get('editing_run_id') == run_id:
                    st.divider()
                    st.markdown(f"#### ✏️ Categorize Laps")
                    
                    state_key = f"lap_cats_{run_id}"
                    laps = processor.get_run_laps(run_id)
                    
                    if laps:
                        if state_key not in st.session_state:
                            # 尝试读取历史记录的每一圈标签
                            saved_cats = meta.get('lap_categories', [])
                            if saved_cats and len(saved_cats) == len(laps):
                                st.session_state[state_key] = saved_cats.copy()
                            else:
                                st.session_state[state_key] = ["Hold Back Easy"] * len(laps)
                                
                        new_name = st.text_input("Run Name", value=meta.get('name', run.get('activityName', '')))
                        notes = st.text_area("Subjective Notes (Optional)", value=meta.get('notes', ''), help="How did the run feel? Any aches, fatigue, or pacing thoughts?")
                        w_num = st.number_input("Week #", value=meta.get('week_num', current_week['week_num']))
                        
                        lap_rows = []
                        for i, lap in enumerate(laps):
                            d_mi = lap.get('distance', 0) / 1609.34
                            t_sec = lap.get('duration', 0)
                            pace = "N/A"
                            if t_sec > 0 and d_mi > 0:
                                p_min = (t_sec / 60) / d_mi
                                pace = f"{int(p_min)}:{int((p_min % 1) * 60):02d}"
                            
                            cat = st.session_state[state_key][i] if i < len(st.session_state[state_key]) else "Hold Back Easy"
                            
                            lap_rows.append({
                                "Lap": i + 1,
                                "Dist (mi)": round(d_mi, 2),
                                "Pace": pace,
                                "Avg HR": lap.get('averageHR', 0),
                                "category": cat 
                            })
                        
                        cat_options = ["Hold Back Easy", "Steady Effort", "Increasing Effort", "Marathon", "LT Effort", "VO2Max", "Sprint", "Rest"]
                        
                        st.markdown("##### ⚡️ Batch Edit Laps")
                        col_batch1, col_batch2, col_batch3 = st.columns([2, 2, 1])
                        with col_batch1:
                            batch_laps = st.multiselect("Select Laps to update:", options=[r["Lap"] for r in lap_rows], key=f"bl_{run_id}")
                        with col_batch2:
                            batch_cat = st.selectbox("Assign Category:", options=cat_options, key=f"bc_{run_id}")
                        with col_batch3:
                            st.write("") 
                            st.write("")
                            if st.button("Apply to Selected", key=f"apply_{run_id}"):
                                for lap_num in batch_laps:
                                    idx = lap_num - 1
                                    st.session_state[state_key][idx] = batch_cat
                                st.rerun() 
                                
                        st.markdown("##### 📝 Individual Laps")
                        edited_laps = st.data_editor(
                            lap_rows,
                            column_config={
                                "category": st.column_config.SelectboxColumn("Category", options=cat_options, required=True)
                            },
                            hide_index=True,
                            key=f"editor_{run_id}"
                        )
                        
                        for i, row in enumerate(edited_laps):
                            if i < len(st.session_state[state_key]):
                                st.session_state[state_key][i] = row['category']
                                
                        st.markdown("<br>", unsafe_allow_html=True)
                        
                        c1, c2, c3 = st.columns([1, 1, 4])
                        with c1:
                            if st.button("Save & Calculate", type="primary", key=f"save_{run_id}"):
                                lap_cats_to_save = []
                                for i, cat in enumerate(st.session_state[state_key]):
                                    laps[i]['category'] = cat
                                    lap_cats_to_save.append(cat)
                                    
                                cat_stats = processor.calculate_category_stats(laps)
                                # 在这里把每一圈的标签列表传给存储函数
                                processor.save_run_metadata(run_id, w_num, new_name, cat_stats, notes=notes, lap_categories=lap_cats_to_save)
                                
                                del st.session_state[state_key]
                                st.session_state['editing_run_id'] = None
                                st.rerun()
                        with c2:
                            if st.button("Cancel", key=f"cancel_{run_id}"):
                                if state_key in st.session_state:
                                    del st.session_state[state_key]
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
                
                # 1. 生成报告 (LangGraph 会自动把对话原封不动存入 data/chat_memory.db)
                report = agent.analyze_health(
                    history_df=df.tail(14), 
                    yesterday_raw=raw_sleep, 
                    thread_id=thread_id
                )
                
                # 2. ⚡️ 将医生的核心裁决提取出来，写进永久的“情景记忆” JSON 中！
                try:
                    import datetime
                    today_str = datetime.date.today().isoformat()
                    processor.save_episodic_memory(
                        activity_id=f"health_check_{today_str}",
                        date=today_str,
                        summary_text=f"Daily Health Check: {report}", # 直接把医生的分析存作永久档案
                        tags=["Daily Health", "Doctor Analysis", "Recovery"]
                    )
                except Exception as e:
                    st.sidebar.warning(f"Failed to save health memory: {e}")

                st.rerun()

        if prompt := st.chat_input("Ask your agents a question...", key="global_chat_input"):
            # 尝试自动解析待确认问题的回答
            if 'memory_engine' in st.session_state:
                _try_resolve_pending(prompt, st.session_state.memory_engine)

            with st.spinner("Supervisor is routing your request..."):
                import datetime
                today_str = datetime.date.today().isoformat()
                
                if not df.empty:
                    latest_health_dict = df.iloc[-1].dropna().to_dict()
                    if 'date' not in latest_health_dict:
                        latest_health_dict['date'] = df.index[-1].isoformat()[:10]
                else:
                    latest_health_dict = {}
                
                recent_miles = df.tail(7)['run_miles'].sum() if not df.empty else 0
                
                # --- 新增：提取最近的 5 条永久记忆 (包含对话建议) ---
                recent_memories = processor.search_episodic_memories(limit=5)
                memory_log = ""
                for m in recent_memories:
                    memory_log += f"- {m['date']}: {m['summary']}\n"
                    if 'coach_advice' in m:
                        memory_log += f"  > 历史讨论建议: {m['coach_advice']}\n"
                
                # 构造全局视角的高密度 JSON
                global_context = {
                    "current_date": today_str,
                    "athlete_status_today": latest_health_dict,
                    "recent_training_load": {
                        "last_7_days_miles": round(recent_miles, 1)
                    },
                    "episodic_memories": memory_log, # 将永久记忆注入！！
                    "instruction": "You are the global coach/doctor. Use the real-time snapshot and EPISODIC MEMORIES to answer."
                }
                
                context_str = f"=== REAL-TIME SNAPSHOT & MEMORIES ===\n{json.dumps(global_context, indent=2, ensure_ascii=False)}"

                # Inject CME concierge prompts BEFORE the main context so LLM sees them first
                if 'memory_engine' in st.session_state:
                    concierge = st.session_state.memory_engine.get_active_concierge_prompts()
                    if concierge:
                        context_str = concierge + "\n\n" + context_str

                agent.chat(
                    user_input=prompt,
                    thread_id=thread_id,
                    system_context=context_str
                )

                # 每次全局对话后自动触发记忆整理，将用户回答沉淀到认知图谱
                agent.consolidate_and_learn(thread_id)
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