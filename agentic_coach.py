import os
import sqlite3
import json
import streamlit as st
from dotenv import load_dotenv
from pathlib import Path
from typing import Annotated, Literal
from typing_extensions import TypedDict

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.sqlite import SqliteSaver

class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]

class AgenticCoach:
    def __init__(self, db_path="data/chat_memory.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        self.api_key = self._find_api_key()
        if not self.api_key:
            print("⚠️ Agentic Coach: No API Key found.")
        else:
            os.environ["GEMINI_API_KEY"] = self.api_key
        
        self.llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.4, api_key=self.api_key)
        self.router_llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.0, api_key=self.api_key)
        
        graph_builder = StateGraph(State)
        graph_builder.add_node("coach", self._coach_node)
        graph_builder.add_node("doctor", self._doctor_node)
        
        graph_builder.add_conditional_edges(
            START, 
            self._route_message, 
            {"coach": "coach", "doctor": "doctor"}
        )
        
        graph_builder.add_edge("coach", END)
        graph_builder.add_edge("doctor", END)
        
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.memory = SqliteSaver(self.conn)
        self.graph = graph_builder.compile(checkpointer=self.memory)

    def _find_api_key(self):
        key = os.getenv("GEMINI_KEY")
        if key: return key
        current_dir = Path(__file__).resolve().parent
        env_path = current_dir / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
            key = os.getenv("GEMINI_KEY")
            if key: return key
        try: return st.secrets["GEMINI_KEY"]
        except: return None

    def _route_message(self, state: State) -> str:
        last_msg = state["messages"][-1].content
        if isinstance(last_msg, list):
            last_msg = "".join([block.get("text", "") for block in last_msg if isinstance(block, dict) and "text" in block])
            
        if "bearing in mind my workouts" in last_msg.lower():
            return "coach"

        prompt = f"""
        You are a routing supervisor. Decide if the following message should be handled by the 'coach' or 'doctor'.
        - COACH: Running, pace, splits, workouts, run analysis, training blocks.
        - DOCTOR: Health, HRV, sleep, stress, resting heart rate, recovery.
        If it's a general greeting or ambiguous, pick 'coach'.
        Output ONLY the exact word 'coach' or 'doctor'.
        
        User message: {last_msg}
        """
        response = self.router_llm.invoke([HumanMessage(content=prompt)])
        
        content = response.content
        if isinstance(content, list):
            content = "".join([block.get("text", "") for block in content if isinstance(block, dict) and "text" in block])
            
        decision = content.strip().lower()
        if "doctor" in decision:
            return "doctor"
        return "coach"

    def _coach_node(self, state: State):
        sys_msg = SystemMessage(content="You are an elite Running Coach and Sports Physiologist. Focus on biomechanics, pace, splits, and training, but ALWAYS connect them to the athlete's overall health and recovery context.")
        messages = [sys_msg] + state["messages"]
        response = self.llm.invoke(messages)
        return {"messages": [response]}

    def _doctor_node(self, state: State):
        sys_msg = SystemMessage(content="You are an elite physiological Health Doctor. Focus on HRV, Sleep Scores, and nervous system recovery. You share a history log with a Running Coach. Acknowledge running data if it explains fatigue, but stick to your domain.")
        messages = [sys_msg] + state["messages"]
        response = self.llm.invoke(messages)
        return {"messages": [response]}

    def chat(self, user_input: str, thread_id: str, system_context: str = None):
        config = {"configurable": {"thread_id": thread_id}}
        messages_to_send = []
        
        if system_context:
            messages_to_send.append(SystemMessage(content=system_context))
            
        messages_to_send.append(HumanMessage(content=user_input))
        
        events = self.graph.stream(
            {"messages": messages_to_send}, 
            config, 
            stream_mode="values"
        )
        
        for event in events:
            final_message = event["messages"][-1]
            
        content = final_message.content
        if isinstance(content, list):
            return "".join([block.get("text", "") for block in content if isinstance(block, dict) and "text" in block])
        return str(content)
        
    def get_history(self, thread_id: str):
        config = {"configurable": {"thread_id": thread_id}}
        try:
            state = self.graph.get_state(config)
            return state.values.get("messages", [])
        except:
            return []

    def follow_up_chat(self, user_input: str, thread_id: str):
        """
        Continues the conversation in an existing thread without injecting 
        the heavy system context prompt again.
        """
        return self.chat(user_input=user_input, thread_id=thread_id, system_context=None)

    def analyze_run(self, context_dict: dict, thread_id: str, telemetry_df=None):
        run_ctx = context_dict['run_context']
        run_name = run_ctx.get('name', 'Unnamed Workout')
        athlete_notes = run_ctx.get('notes', '').strip()
        
        zones_text = "None Available"
        if run_ctx.get('hr_zones'):
            zones = run_ctx['hr_zones']
            zones_text = "\n".join([f"- {z['name']}: {z['range']}" for z in zones])

        perf_data = json.dumps(run_ctx.get('category_stats', []), indent=2)

        fatigue = "None reported."
        if context_dict.get('auxiliary_activities_last_7d'):
            fatigue = "\n".join([f"- {a['date']}: {a['type']} ({a['desc']})" for a in context_dict['auxiliary_activities_last_7d']])

        # --- TELEMETRY CONTEXT ---
        telemetry_section = ""
        if telemetry_df is not None and not telemetry_df.empty:
            csv_data = telemetry_df.to_csv(index=False)
            telemetry_section = f"\n\n**RAW TELEMETRY (Lap-by-Lap Downsampled Data):**\n```csv\n{csv_data}\n```"

        # --- SUBJECTIVE NOTES CONTEXT ---
        notes_section = ""
        if athlete_notes:
            notes_section = f"\n- **Athlete's Subjective Notes:** \"{athlete_notes}\""

        system_instructions = f"""
        ACT AS AN ELITE SPORTS DATA SCIENTIST AND PHYSIOLOGIST.

        **THE PHILOSOPHY:**
        1. **Subjective Feel is GROUND TRUTH:** The athlete has manually categorized the effort levels.
        2. **Metrics are Malleable:** Heart rate varies by day based on health, elevation changes, and fatigue.
        3. **Goal:** Evaluate how the objective telemetry matched the intended workout purpose.

        **DATA INPUTS:**
        - **Workout Purpose / Title:** "{run_name}" 
        - **Baseline Map (The Theory):** {zones_text}
        - **Run Data / Assigned Categories (The Reality):** {perf_data}
        - **Context (Fatigue/Auxiliary):** {fatigue}{telemetry_section}{notes_section}

        **INSTRUCTIONS:**
        1. **Analyze Intent vs Execution:** Use the "Workout Purpose / Title" to frame your analysis. 
        2. **Subjective Notes Context:** If 'Athlete's Subjective Notes' are provided, use them as the primary context for how the run physically felt. If no notes are provided, rely purely on the objective telemetry and do NOT mention the lack of notes.
        3. **TELEMETRY ANALYSIS (CRITICAL):**
           - Evaluate the shape of the curves (Pace, Cadence, Elevation) for the identified laps.
           - For Speed, VO2Max, or LT intervals, RECOGNIZE CARDIAC LAG. The average HR will be artificially low. Judge interval success by the PEAK HR and END HR found within that Lap's telemetry block. Explicitly call out cardiac lag if present.
        4. **Map the Drift:** Identify the **Actual Average HR** (or Peak HR for speed intervals) for the efforts. Compare it to the Baseline Zone. Flag differences > 5 bpm as a **Significant Drift** 🚨. 

        **OUTPUT FORMAT (Markdown):**
        ### 🧠 Workout Analysis: {run_name}
        *(Detailed observation of how the execution matched the workout's purpose and the subjective notes. Discuss specific Lap numbers, curve shapes, and fatigue impacts).*
        
        ### 🗺️ Proposed Map (Today's Reality)
        | Effort Category | Baseline Zone | **Proposed Mapping** | Drift |
        | :--- | :--- | :--- | :--- |
        | [Category Name] | [e.g. 145-160] | **[Actual/Peak HR]** | [e.g. +7 bpm 🚨] |
        
        ### 💡 Recommendation
        *(Provide an actionable physiological takeaway).*
        """

        run_date = run_ctx.get('date', 'today')
        user_message = f"Please analyze my execution on {run_date} for the run titled '{run_name}'."

        return self.chat(
            user_input=user_message, 
            thread_id=thread_id, 
            system_context=system_instructions
        )

    def analyze_health(self, history_df, yesterday_raw, thread_id: str):
        import datetime
        today_str = datetime.date.today().isoformat()
        yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        
        trends = history_df.to_markdown()

        sleep_dto = yesterday_raw.get('dailySleepDTO', {})
        sleep_details = {
            "deep_sleep_min": sleep_dto.get('deepSleepSeconds', 0) / 60,
            "rem_sleep_min": sleep_dto.get('remSleepSeconds', 0) / 60,
            "awake_min": sleep_dto.get('awakeSleepSeconds', 0) / 60,
            "feedback": sleep_dto.get('sleepScoreFeedback'),
            "stress_during_sleep": yesterday_raw.get('avgSleepStress')
        }

        system_instructions = f"""
        ACT AS A HOLISTIC HEALTH & PERFORMANCE DOCTOR.
        
        **OBJECTIVE:** Analyze the athlete's recovery status for TODAY ({today_str}) based on LONG-TERM TRENDS and YESTERDAY'S SLEEP ({yesterday_str}).

        **DATA SOURCE 1: 14-Day History (CSV)**
        {trends}
        *(Columns: sleep_score, rhr, hrv, run_miles, stress)*

        **DATA SOURCE 2: Last Night's Deep Dive (JSON Extract)**
        - Deep Sleep: {sleep_details['deep_sleep_min']:.0f} mins
        - REM Sleep: {sleep_details['rem_sleep_min']:.0f} mins
        - Awake/Restless: {sleep_details['awake_min']:.0f} mins
        - Garmin Feedback: "{sleep_details['feedback']}"
        - Overnight Stress: {sleep_details['stress_during_sleep']} (Low is good)

        **ANALYSIS REQUIRED (Markdown):**
        
        ### 📉 Trend Detection
        *Look at the 14-day history. Is RHR trending up? Is HRV crashing? How does Sleep Score correlate with Run Miles?*
        *Be specific.*

        ### 🛌 Last Night's Quality
        *Don't just look at the score. Look at Deep vs. REM. Is the athlete physically recovered (Deep) but mentally tired (REM)?*

        ### 🚦 Readiness Verdict
        *Synthesize everything into a training recommendation for {today_str}.*
        *Options: [GREEN LIGHT: Push Hard], [YELLOW LIGHT: Aerobic Only], [RED LIGHT: Rest].*
        """

        user_message = f"Please analyze my health condition for today ({today_str}), bearing in mind my recent workouts."

        return self.chat(
            user_input=user_message, 
            thread_id=thread_id, 
            system_context=system_instructions
        )