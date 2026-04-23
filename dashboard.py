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

# --- CME Topic Decision Queue ---
# When consolidation can't auto-match an LLM proposal to an existing topic
# (cosine < MATCH_THRESHOLD), it parks the proposal here. User resolves:
# merge into an existing topic, create as a new topic, or reject.
memory_engine = st.session_state.memory_engine
_pending_decisions = memory_engine.list_pending_decisions()
if _pending_decisions:
    with st.expander(
        f"🧠 Memory decisions waiting ({len(_pending_decisions)})",
        expanded=False,
    ):
        st.caption(
            "The AI proposed new memory entries that didn't strongly match any "
            "existing topic. Pick the right action for each."
        )
        for decision in _pending_decisions:
            did = decision["decision_id"]
            kind = decision["kind"]
            proposal = decision["proposal"]
            candidates = decision["candidates"]

            st.markdown("---")

            if kind == "episode_linking":
                st.markdown(f"**[episode_linking]** {proposal.get('what', '')[:80]}")
                if proposal.get("lesson_learned"):
                    st.markdown(f"_lesson:_ {proposal['lesson_learned']}")
                st.caption(f"event_type: {proposal.get('event_type', '')}")

                all_topics = memory_engine.list_topics()
                tid_to_label = {
                    t["topic_id"]: f"[{t['topic_id']}] {t['name']} ({t['status']})"
                    for t in all_topics
                }
                picked = st.multiselect(
                    "Pick topics this episode belongs to (leave empty to keep unlinked)",
                    options=list(tid_to_label.keys()),
                    format_func=lambda t: tid_to_label[t],
                    key=f"dec_link_{did}",
                )
                col1, col2 = st.columns(2)
                if col1.button("Link", key=f"dec_link_btn_{did}"):
                    memory_engine.resolve_topic_decision(
                        did, "link", target_topic_ids=picked
                    )
                    if picked:
                        st.toast(f"🔗 Linked to {len(picked)} topic(s)")
                    else:
                        st.toast("Kept unlinked")
                    st.rerun()
                if col2.button("Reject", key=f"dec_reject_{did}"):
                    memory_engine.resolve_topic_decision(did, "reject")
                    st.toast("🗑️ Rejected")
                    st.rerun()
                continue

            header = (
                f"**[{kind}]** {proposal.get('name') or proposal.get('subject_summary') or proposal.get('question_for_user', '')[:60]}"
            )
            st.markdown(header)
            if proposal.get("working_conclusion"):
                st.markdown(f"_conclusion:_ {proposal['working_conclusion']}")
            if proposal.get("question_for_user"):
                st.markdown(f"_question:_ {proposal['question_for_user']}")

            if candidates:
                st.markdown("**Top candidates** (pick one to merge into):")
                label_to_tid: dict[str, str] = {}
                for c in candidates[:5]:
                    label = f"{c['score']:.3f}  [{c['topic_id']}]  {c['name']} ({c['status']})"
                    label_to_tid[label] = c["topic_id"]
                chosen_label = st.radio(
                    "candidate",
                    options=list(label_to_tid.keys()),
                    key=f"dec_radio_{did}",
                    label_visibility="collapsed",
                )
                chosen_tid = label_to_tid[chosen_label]
            else:
                chosen_tid = None
                st.caption("(no candidates — can only create-new or reject)")

            col1, col2, col3 = st.columns(3)
            if col1.button("Merge into selected", key=f"dec_merge_{did}", disabled=chosen_tid is None):
                memory_engine.resolve_topic_decision(did, "merge", target_topic_id=chosen_tid)
                st.toast(f"✅ Merged into {chosen_tid}")
                st.rerun()
            if col2.button("Create new", key=f"dec_create_{did}"):
                new_tid = memory_engine.resolve_topic_decision(did, "create_new")
                st.toast(f"➕ Created {new_tid}")
                st.rerun()
            if col3.button("Reject", key=f"dec_reject_{did}"):
                memory_engine.resolve_topic_decision(did, "reject")
                st.toast("🗑️ Rejected")
                st.rerun()

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

    # ==========================================
    # TRAINING CYCLE SUMMARY + WEEKLY SUMMARY
    # ==========================================
    @st.cache_data(ttl=300)
    def _compute_cycle_and_week_stats(_processor, block_start, block_end, week_start, week_end, all_week_labels):
        """Compute training cycle aggregate and current week stats."""
        from collections import defaultdict

        # --- Cycle-wide stats ---
        all_runs_raw = _processor.get_activities_in_range(block_start, block_end)
        all_runs = [r for r in all_runs_raw if 'running' in r.get('activityType', {}).get('typeKey', '')]

        cycle_miles = 0
        cycle_time_sec = 0
        cycle_elevation_m = 0
        cycle_calories = 0
        cycle_hrs = []
        cat_totals = defaultdict(lambda: {'dist_m': 0, 'time_s': 0, 'hr_weighted': 0, 'pace_weighted': 0, 'elev_m': 0})
        longest_run_mi = 0

        for r in all_runs:
            dist_m = r.get('distance', 0)
            dur_s = r.get('movingDuration', 0) or r.get('duration', 0)
            hr = r.get('averageHR', 0) or 0
            elev = r.get('elevationGain', 0) or 0
            cal = r.get('calories', 0) or 0

            mi = dist_m / 1609.34
            cycle_miles += mi
            cycle_time_sec += dur_s
            cycle_elevation_m += elev
            cycle_calories += cal
            if hr > 0:
                cycle_hrs.append(hr)
            if mi > longest_run_mi:
                longest_run_mi = mi

            meta = r.get('manual_meta', {})
            cs = meta.get('category_stats')
            if cs:
                for cat in cs:
                    c = cat['category']
                    cat_totals[c]['dist_m'] += cat['distance_mi'] * 1609.34
                    cat_totals[c]['hr_weighted'] += cat.get('avg_hr', 0) * cat['distance_mi']
                    pace_str = cat.get('pace', '')
                    if pace_str and ':' in pace_str:
                        parts = pace_str.split(':')
                        pace_dec = int(parts[0]) + int(parts[1]) / 60
                        cat_totals[c]['pace_weighted'] += pace_dec * cat['distance_mi']

                # Accumulate elevation per effort from lap-level data
                lap_cats = meta.get('lap_categories', [])
                if lap_cats:
                    laps = _processor.get_run_laps(r['activityId'])
                    for i, lap in enumerate(laps):
                        if i < len(lap_cats):
                            c = lap_cats[i]
                            cat_totals[c]['elev_m'] += lap.get('elevationGain', 0) or 0

        cycle_avg_hr = sum(cycle_hrs) / len(cycle_hrs) if cycle_hrs else 0
        cycle_pace_dec = (cycle_time_sec / (cycle_miles * 60)) if cycle_miles > 0 else 0
        cycle_pace_str = f"{int(cycle_pace_dec)}:{int((cycle_pace_dec % 1) * 60):02d}" if cycle_miles > 0 else "N/A"

        # Category breakdown sorted by distance
        cat_rows = []
        for cat, v in sorted(cat_totals.items(), key=lambda x: -x[1]['dist_m']):
            cat_mi = v['dist_m'] / 1609.34
            cat_hr = int(v['hr_weighted'] / cat_mi) if cat_mi > 0 else 0
            pct = (cat_mi / cycle_miles * 100) if cycle_miles > 0 else 0
            if cat_mi > 0 and v['pace_weighted'] > 0:
                avg_pace = v['pace_weighted'] / cat_mi
                pace_str = f"{int(avg_pace)}:{int((avg_pace % 1) * 60):02d}"
            else:
                pace_str = "—"
            elev_ft = int(v['elev_m'] * 3.281)
            cat_rows.append({
                "Effort": cat,
                "Miles": round(cat_mi, 1),
                "% of Total": f"{pct:.0f}%",
                "Avg Pace": pace_str,
                "Avg HR": cat_hr if cat_hr > 0 else "—",
                "Elev (ft)": f"{elev_ft:,}" if elev_ft > 0 else "—",
            })

        # --- Weekly mileage trend (for sparkline context) ---
        weekly_miles = []
        for w in all_week_labels:
            w_runs_raw = _processor.get_activities_in_range(w['start'], w['end'])
            w_runs = [r for r in w_runs_raw if 'running' in r.get('activityType', {}).get('typeKey', '')]
            wm = sum(r.get('distance', 0) for r in w_runs) / 1609.34
            weekly_miles.append({"Week": f"W{w['week_num']}", "Miles": round(wm, 1)})

        # --- Current week stats ---
        wk_runs_raw = _processor.get_activities_in_range(week_start, week_end)
        wk_runs = [r for r in wk_runs_raw if 'running' in r.get('activityType', {}).get('typeKey', '')]
        wk_miles = sum(r.get('distance', 0) for r in wk_runs) / 1609.34
        wk_time_sec = sum(r.get('movingDuration', 0) or r.get('duration', 0) for r in wk_runs)
        wk_hrs = [r.get('averageHR', 0) for r in wk_runs if r.get('averageHR')]
        wk_elev = sum(r.get('elevationGain', 0) or 0 for r in wk_runs)
        wk_avg_hr = sum(wk_hrs) / len(wk_hrs) if wk_hrs else 0
        wk_pace_dec = (wk_time_sec / (wk_miles * 60)) if wk_miles > 0 else 0
        wk_pace_str = f"{int(wk_pace_dec)}:{int((wk_pace_dec % 1) * 60):02d}" if wk_miles > 0 else "N/A"

        # Week category breakdown
        wk_cat_totals = defaultdict(float)
        for r in wk_runs:
            meta = r.get('manual_meta', {})
            cs = meta.get('category_stats')
            if cs:
                for cat in cs:
                    wk_cat_totals[cat['category']] += cat['distance_mi']

        # Compare to cycle average per week
        elapsed_weeks = max(1, current_week['week_num'])
        avg_weekly_miles = cycle_miles / elapsed_weeks if elapsed_weeks > 0 else 0

        return {
            'cycle': {
                'total_runs': len(all_runs),
                'total_miles': round(cycle_miles, 1),
                'total_hours': round(cycle_time_sec / 3600, 1),
                'avg_pace': cycle_pace_str,
                'avg_hr': int(cycle_avg_hr),
                'elevation_ft': int(cycle_elevation_m * 3.281),
                'calories': int(cycle_calories),
                'longest_run': round(longest_run_mi, 1),
                'avg_weekly_miles': round(avg_weekly_miles, 1),
                'cat_rows': cat_rows,
            },
            'week': {
                'runs': len(wk_runs),
                'miles': round(wk_miles, 1),
                'hours': round(wk_time_sec / 3600, 1),
                'avg_pace': wk_pace_str,
                'avg_hr': int(wk_avg_hr),
                'elevation_ft': int(wk_elev * 3.281),
                'cat_totals': dict(wk_cat_totals),
                'vs_avg': round(wk_miles - avg_weekly_miles, 1),
            },
            'weekly_miles': weekly_miles,
        }

    stats = _compute_cycle_and_week_stats(
        processor, current_block['start_date'], current_block['end_date'],
        current_week['start'], current_week['end'], weeks
    )
    cy = stats['cycle']
    wk = stats['week']

    # --- Block 1: Training Cycle Overview ---
    with st.expander(f"📊 Training Cycle Overview — {current_block['name']}", expanded=False):
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Miles", f"{cy['total_miles']}")
        m2.metric("Total Runs", cy['total_runs'])
        m3.metric("Total Hours", cy['total_hours'])
        m4.metric("Avg Weekly Miles", cy['avg_weekly_miles'])

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Avg Pace", cy['avg_pace'])
        m6.metric("Avg HR", cy['avg_hr'])
        m7.metric("Elevation", f"{cy['elevation_ft']:,} ft")
        m8.metric("Longest Run", f"{cy['longest_run']} mi")

        if cy['cat_rows']:
            st.markdown("**Effort Distribution** (categorized runs)")
            st.dataframe(pd.DataFrame(cy['cat_rows']), hide_index=True, use_container_width=True)

        # Weekly mileage mini-chart
        wm_df = pd.DataFrame(stats['weekly_miles'])
        chart = alt.Chart(wm_df).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X('Week:N', sort=None, title=None),
            y=alt.Y('Miles:Q', title='Miles'),
            color=alt.condition(
                alt.datum.Week == f"W{current_week['week_num']}",
                alt.value('#ff4b4b'),
                alt.value('#4e8cff')
            ),
            tooltip=['Week', 'Miles']
        ).properties(height=180, title="Weekly Mileage Progression")
        st.altair_chart(chart, use_container_width=True)

    # --- Block 2: This Week Summary ---
    if wk['runs'] > 0:
        vs_label = f"{wk['vs_avg']:+.1f} vs avg" if wk['vs_avg'] != 0 else "on avg"
        st.info(f"**Week Stats:** {wk['runs']} Runs | {wk['miles']} Miles ({vs_label}) | Pace {wk['avg_pace']} | HR {wk['avg_hr']} | ↑ {wk['elevation_ft']:,} ft")
    else:
        st.info("**Week Stats:** No runs this week yet")

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

                    # Agent dropdown — Training Log tab defaults to Coach
                    _run_agent_label_to_key = {"🏃 Coach (running, pace, training)": "coach", "❤️ Doctor (recovery, HRV, sleep)": "doctor"}
                    _selected_run_agent = st.selectbox(
                        "Who should answer?",
                        options=list(_run_agent_label_to_key.keys()),
                        index=0,  # Coach default on this tab
                        key=f"agent_selector_run_{run_id}",
                    )
                    active_agent_run = _run_agent_label_to_key[_selected_run_agent]

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
                                response = agent.follow_up_chat(user_input=run_prompt, thread_id=f"run_analysis_{run_id}", agent=active_agent_run)
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
    st.header("Recovery & Health")

    # `df` (historical ledger) is still needed by the AI Co-Pilot below.
    stats = processor.get_health_stats()
    df = pd.DataFrame(stats) if stats else pd.DataFrame()
    if not df.empty:
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)

    # ==================================================================
    # BLOCK 1 — Last night sleep + basic trend
    # ==================================================================
    st.subheader("🛌 Last Night")
    sleep_data = processor.get_last_night_sleep()

    if not sleep_data:
        st.info("No recent sleep data. Sync Garmin first.")
    else:
        # One row per stage — bar length = minutes, label on left, 7d avg as tick
        avg7 = sleep_data.get('avg_7d', {})
        stage_df = pd.DataFrame([
            {'stage': 'Deep',  'minutes': sleep_data['deep_min'],  'avg7d': avg7.get('deep_min')  or 0},
            {'stage': 'REM',   'minutes': sleep_data['rem_min'],   'avg7d': avg7.get('rem_min')   or 0},
            {'stage': 'Light', 'minutes': sleep_data['light_min'], 'avg7d': avg7.get('light_min') or 0},
            {'stage': 'Awake', 'minutes': sleep_data['awake_min'], 'avg7d': avg7.get('awake_min') or 0},
        ])
        stage_order = ['Deep', 'REM', 'Light', 'Awake']
        stage_colors = ['#1f4e8c', '#5b9bd5', '#a9cce3', '#ec7063']

        bars = alt.Chart(stage_df).mark_bar().encode(
            y=alt.Y('stage:N', sort=stage_order, title=None),
            x=alt.X('minutes:Q', title='Minutes'),
            color=alt.Color('stage:N',
                            scale=alt.Scale(domain=stage_order, range=stage_colors),
                            legend=None),
            tooltip=[
                alt.Tooltip('stage:N', title='Stage'),
                alt.Tooltip('minutes:Q', title='Last Night (min)'),
                alt.Tooltip('avg7d:Q', title='7-Day Avg (min)', format='.0f'),
            ],
        )
        # Tick mark showing 7-day average, so you can see last night vs baseline at a glance
        avg_ticks = alt.Chart(stage_df).mark_tick(
            color='#333', thickness=2, size=24,
        ).encode(
            y=alt.Y('stage:N', sort=stage_order),
            x=alt.X('avg7d:Q'),
            tooltip=[alt.Tooltip('avg7d:Q', title='7-Day Avg (min)', format='.0f')],
        )
        labels = alt.Chart(stage_df).mark_text(
            align='left', baseline='middle', dx=6,
        ).encode(
            y=alt.Y('stage:N', sort=stage_order),
            x=alt.X('minutes:Q'),
            text=alt.Text('minutes:Q', format='.0f'),
        )
        stage_chart = alt.layer(bars, avg_ticks, labels).properties(height=180)
        st.altair_chart(stage_chart, use_container_width=True)

        hours = sleep_data['total_min'] // 60
        mins = sleep_data['total_min'] % 60
        st.caption(
            f"Total sleep: {hours}h {mins}m  •  "
            f"Colored bar = last night minutes · black tick = 7-day avg"
        )

        # Sleep signals row
        sig1, sig2, sig3 = st.columns(3)
        with sig1:
            resp = sleep_data.get('avg_respiration')
            delta = None
            if resp is not None and avg7.get('avg_respiration') is not None:
                delta = f"{resp - avg7['avg_respiration']:+.1f} vs 7d"
            st.metric("Sleep Respiration (bpm)", f"{resp:.0f}" if resp else "—", delta=delta)
        with sig2:
            stress = sleep_data.get('sleep_stress')
            delta = None
            if stress is not None and avg7.get('sleep_stress') is not None:
                delta = f"{stress - avg7['sleep_stress']:+.1f} vs 7d"
            # Lower stress is better — invert color so ↑ reads red, ↓ reads green
            st.metric("Sleep Stress", f"{stress:.0f}" if stress is not None else "—",
                      delta=delta, delta_color="inverse")
        with sig3:
            total = sleep_data['total_min']
            delta = None
            if avg7.get('total_min'):
                delta = f"{int(total - avg7['total_min']):+d} min vs 7d"
            st.metric("Total Sleep (min)", total, delta=delta)

    st.markdown("**28-Day Trend — Resting Heart Rate & Sleep Score**")
    if not df.empty:
        trend_df = (df[['rhr', 'sleep_score']]
                    .tail(28)
                    .rename(columns={'rhr': 'Resting Heart Rate', 'sleep_score': 'Sleep Score'})
                    .reset_index()
                    .melt('date', var_name='metric', value_name='value')
                    .dropna(subset=['value']))
        trend_chart = alt.Chart(trend_df).mark_line(point=True).encode(
            x=alt.X('date:T', axis=alt.Axis(format='%b %d', title=None, labelAngle=-30)),
            y=alt.Y('value:Q', title=None),
            color=alt.Color('metric:N', legend=alt.Legend(title=None, orient='bottom')),
            tooltip=[alt.Tooltip('date:T', format='%b %d'), 'metric:N', alt.Tooltip('value:Q', format='.1f')],
        ).properties(height=240)
        st.altair_chart(trend_chart, use_container_width=True)
    else:
        st.info("No historical data yet.")

    st.divider()

    # ==================================================================
    # BLOCK 2 — HRV Status + Body Battery
    # ==================================================================
    st.subheader("🫀 HRV & Body Battery")

    if not df.empty and 'hrv' in df.columns:
        # HRV chart — baseline band + colored status dots
        df['hrv_7d'] = df['hrv'].rolling(window=7, min_periods=1).mean()
        df['baseline_mean'] = df['hrv'].rolling(window=21, min_periods=1).mean()
        df['baseline_std'] = df['hrv'].rolling(window=21, min_periods=1).std().clip(lower=3.5)
        df['baseline_high'] = df['baseline_mean'] + df['baseline_std']
        df['baseline_low'] = df['baseline_mean'] - df['baseline_std']

        def _hrv_status(row):
            if pd.isna(row['hrv_7d']) or pd.isna(row['baseline_low']):
                return "Range"
            if row['hrv_7d'] < row['baseline_low'] or row['hrv_7d'] > row['baseline_high']:
                return "Unbalanced"
            return "Balanced"

        df['hrv_status'] = df.apply(_hrv_status, axis=1)
        chart_df = df.reset_index().dropna(subset=['hrv_7d'])

        date_axis = alt.Axis(format='%b %d', title=None, labelAngle=-30)
        baseline_band = alt.Chart(chart_df).mark_area(opacity=0.15, color='#888888').encode(
            x=alt.X('date:T', axis=date_axis),
            y=alt.Y('baseline_low:Q', title='HRV (ms)', scale=alt.Scale(zero=False)),
            y2='baseline_high:Q'
        )
        hrv_line = alt.Chart(chart_df).mark_line(color='#A0A0A0', size=1.5).encode(
            x=alt.X('date:T', axis=date_axis), y='hrv_7d:Q')
        hrv_points = alt.Chart(chart_df).mark_circle(size=80).encode(
            x=alt.X('date:T', axis=date_axis), y='hrv_7d:Q',
            color=alt.Color('hrv_status:N',
                            scale=alt.Scale(domain=['Balanced','Unbalanced','Range'],
                                            range=['#2ca02c','#d62728','#7f7f7f']),
                            legend=alt.Legend(title="7-Day Avg Status")),
            tooltip=[
                alt.Tooltip('date:T', format='%b %d', title='Date'),
                alt.Tooltip('hrv_7d:Q', title='7d Avg', format='.1f'),
                alt.Tooltip('hrv:Q', title='Last Night', format='.1f'),
                alt.Tooltip('hrv_status:N', title='Status'),
            ]
        )
        st.altair_chart(
            alt.layer(baseline_band, hrv_line, hrv_points).properties(height=260).interactive(),
            use_container_width=True,
        )
        st.caption("Gray band = Garmin's expected HRV range (21-day baseline ± 1σ). Dots show 7-day rolling average colored by whether it sits inside or outside that range.")

    bb_df = processor.get_body_battery_series(days=14)
    if bb_df.empty:
        st.info("No Body Battery history available.")
    else:
        latest_bb = bb_df.iloc[-1]
        bb1, bb2, bb3 = st.columns(3)
        with bb1: st.metric("Current", int(latest_bb['current']) if pd.notna(latest_bb['current']) else "—")
        with bb2: st.metric("Wake Level", int(latest_bb['wake']) if pd.notna(latest_bb['wake']) else "—")
        with bb3:
            charged = latest_bb['charged']
            drained = latest_bb['drained']
            net = (charged or 0) - (drained or 0)
            st.metric("Overnight Net", f"{net:+d}" if net else "—",
                      delta=f"+{int(charged)} / -{int(drained)}" if pd.notna(charged) and pd.notna(drained) else None,
                      delta_color="off")

        bb_long = bb_df.melt(id_vars='date', value_vars=['wake', 'lowest', 'current'],
                             var_name='metric', value_name='value').dropna(subset=['value'])
        bb_chart = alt.Chart(bb_long).mark_line(point=True).encode(
            x=alt.X('date:T', axis=alt.Axis(format='%b %d', title=None, labelAngle=-30)),
            y=alt.Y('value:Q', title='Body Battery', scale=alt.Scale(domain=[0, 100])),
            color=alt.Color('metric:N', legend=alt.Legend(title=None, orient='bottom')),
            tooltip=[alt.Tooltip('date:T', format='%b %d'), 'metric:N', alt.Tooltip('value:Q', format='.0f')],
        ).properties(height=220)
        st.altair_chart(bb_chart, use_container_width=True)

    st.divider()

    # ==================================================================
    # BLOCK 3 — Training load (status / VO2 / intensity / fitness age)
    # ==================================================================
    st.subheader("💪 Training Load")
    status = processor.get_training_status_today()
    intensity = processor.get_weekly_intensity()
    fitness_age = processor.get_fitness_age()
    vo2_df = processor.get_vo2_max_series(days=30)

    row1_col1, row1_col2 = st.columns(2)
    with row1_col1:
        if status:
            label = processor.describe_training_status(status.get('status_code'))
            st.metric("Training Status", label)
            if status.get('acwr_ratio') is not None:
                ratio = status['acwr_ratio']
                acwr_status = status.get('acwr_status') or ''
                st.caption(
                    f"Acute:Chronic Workload Ratio: **{ratio:.2f}** ({acwr_status.title()})"
                )
                # Typical safe window: 0.8 – 1.3; clip for the bar
                st.progress(min(max(ratio / 2.0, 0.0), 1.0))
        else:
            st.info("No training status data.")
    with row1_col2:
        if vo2_df.empty or pd.isna(vo2_df['vo2_max'].iloc[-1]):
            st.info("No VO2 Max data.")
        else:
            current_vo2 = vo2_df['vo2_max'].iloc[-1]
            delta = None
            if len(vo2_df) > 1:
                delta = f"{current_vo2 - vo2_df['vo2_max'].iloc[0]:+.1f} vs 30d ago"
            st.metric("VO2 Max", f"{current_vo2:.1f}", delta=delta)
            vo2_chart = alt.Chart(vo2_df).mark_line(point=True, color='#5b9bd5').encode(
                x=alt.X('date:T', axis=alt.Axis(format='%b %d', title=None, labelAngle=-30)),
                y=alt.Y('vo2_max:Q', scale=alt.Scale(zero=False), title=None),
                tooltip=[alt.Tooltip('date:T', format='%b %d'), alt.Tooltip('vo2_max:Q', format='.1f')],
            ).properties(height=140)
            st.altair_chart(vo2_chart, use_container_width=True)

    row2_col1, row2_col2 = st.columns(2)
    with row2_col1:
        if intensity:
            st.markdown("**Weekly Intensity Minutes**")
            pct = min(intensity['percent'] / 100, 1.0)
            st.progress(pct, text=f"{intensity['total_min']} / {intensity['goal_min']} min  ({intensity['percent']}%)")
            st.caption(f"Moderate: {intensity['moderate_min']}  •  Vigorous: {intensity['vigorous_min']}")
        else:
            st.info("No intensity data.")
    with row2_col2:
        if fitness_age and fitness_age.get('fitness') is not None:
            actual = fitness_age['chronological']
            fit = fitness_age['fitness']
            delta_years = fit - actual
            st.metric("Fitness Age", f"{fit:.1f}",
                      delta=f"{delta_years:+.1f} vs actual ({actual})",
                      delta_color="inverse")
            if fitness_age.get('achievable'):
                st.caption(f"Achievable: {fitness_age['achievable']:.1f}")
        else:
            st.info("No fitness age data.")

    st.divider()

    # ==================================================================
    # BLOCK 4 — Today's Training Readiness + factor breakdown
    # ==================================================================
    st.subheader("🚦 Today's Training Readiness")
    readiness = processor.get_training_readiness_today()

    if not readiness:
        st.info("No readiness data yet today.")
    else:
        level = readiness.get('level') or 'UNKNOWN'
        badge = {"HIGH": "🟢", "MODERATE": "🟡", "LOW": "🔴"}.get(level, "⚪️")
        score = readiness.get('score')

        r1, r2 = st.columns([1, 2])
        with r1:
            st.metric("Score", f"{score}" if score is not None else "—", delta=f"{badge} {level}")
        with r2:
            feedback = processor.describe_readiness_feedback(
                readiness.get('feedback_short'), readiness.get('feedback_long')
            )
            if feedback:
                st.markdown(f"> 💬 {feedback}")

        st.markdown("**Factor Breakdown**  _(higher = more favorable)_")
        factors = readiness.get('factors', {})
        for name, pct in factors.items():
            if pct is None:
                continue
            st.progress(pct / 100, text=f"{name}  —  {pct}%")

    st.divider()

    # ==================================================================
    # BLOCK 5 — AI Co-Pilot (unchanged)
    # ==================================================================
    if not df.empty:
        # ==========================================
        # 4. LANGGRAPH AGENT CHATBOX
        # ==========================================
        st.divider()
        st.subheader("🤖 Unified AI Co-Pilot")
        st.caption("Pick who answers, then ask away. Defaults to Doctor here — switch to Coach for training questions.")

        # Agent dropdown — Recovery & Health tab defaults to Doctor
        _agent_label_to_key = {"❤️ Doctor (recovery, HRV, sleep)": "doctor", "🏃 Coach (running, pace, training)": "coach"}
        _selected_agent_health = st.selectbox(
            "Who should answer?",
            options=list(_agent_label_to_key.keys()),
            index=0,  # Doctor default on this tab
            key="agent_selector_health",
        )
        active_agent_health = _agent_label_to_key[_selected_agent_health]

        if st.button("🩺 Analyze Today's Health"):
            with st.spinner("Doctor is reviewing your charts..."):                
                yesterday_str = (datetime.date.today()).isoformat()
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
                    system_context=context_str,
                    agent=active_agent_health,
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