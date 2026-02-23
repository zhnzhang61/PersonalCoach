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
        
        # --- API KEY LOGIC ---
        self.api_key = self._find_api_key()
        if not self.api_key:
            print("⚠️ Agentic Coach: No API Key found.")
        else:
            os.environ["GEMINI_API_KEY"] = self.api_key
        
        # 1. Initialize the LLMs
        #gemini-2.5-flash
        #gemini-flash-latest
        self.llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.4, api_key=self.api_key)
        self.router_llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.0, api_key=self.api_key)
        
        # 2. Build the Graph
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
        
        # 3. Compile with Checkpointer
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

    # --- THE SUPERVISOR ---
    def _route_message(self, state: State) -> str:
        """Reads the last message and decides which expert to wake up."""
        last_msg = state["messages"][-1].content
        
        # Ensure we are extracting text if the last message is a list block
        if isinstance(last_msg, list):
            last_msg = "".join([block.get("text", "") for block in last_msg if isinstance(block, dict) and "text" in block])
            
        prompt = f"""
        You are a routing supervisor. Decide if the following message should be handled by the 'coach' or 'doctor'.
        - COACH: Running, pace, splits, workouts, run analysis, training blocks.
        - DOCTOR: Health, HRV, sleep, stress, resting heart rate, recovery.
        If it's a general greeting or ambiguous, pick 'coach'.
        Output ONLY the exact word 'coach' or 'doctor'.
        
        User message: {last_msg}
        """
        response = self.router_llm.invoke([HumanMessage(content=prompt)])
        
        # --- BUG FIX: Extract text safely before calling .strip() ---
        content = response.content
        if isinstance(content, list):
            content = "".join([block.get("text", "") for block in content if isinstance(block, dict) and "text" in block])
            
        decision = content.strip().lower()
        if "doctor" in decision:
            return "doctor"
        return "coach"

    # --- THE EXPERTS ---
    def _coach_node(self, state: State):
        sys_msg = SystemMessage(content="You are an elite Running Coach. Focus on biomechanics, pace, splits, and training. You share a history log with a Health Doctor. Acknowledge health data if relevant to the run, but stick to your domain.")
        messages = [sys_msg] + state["messages"]
        response = self.llm.invoke(messages)
        return {"messages": [response]}

    def _doctor_node(self, state: State):
        sys_msg = SystemMessage(content="You are an elite physiological Health Doctor. Focus on HRV, Sleep Scores, and nervous system recovery. You share a history log with a Running Coach. Acknowledge running data if it explains fatigue, but stick to your domain.")
        messages = [sys_msg] + state["messages"]
        response = self.llm.invoke(messages)
        return {"messages": [response]}

    # --- THE I/O LOGIC ---
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

    def analyze_run(self, context_dict: dict, thread_id: str):
        run_ctx = context_dict['run_context']
        
        zones_text = "None Available"
        if run_ctx.get('hr_zones'):
            zones = run_ctx['hr_zones']
            zones_text = "\n".join([f"- {z['name']}: {z['range']}" for z in zones])

        perf_data = json.dumps(run_ctx.get('category_stats', []), indent=2)

        fatigue = "None reported."
        if context_dict.get('auxiliary_activities_last_7d'):
            fatigue = "\n".join([f"- {a['date']}: {a['type']} ({a['desc']})" for a in context_dict['auxiliary_activities_last_7d']])

        system_instructions = f"""
        ACT AS AN ELITE SPORTS DATA SCIENTIST.

        **THE PHILOSOPHY:**
        1. **Subjective Feel is GROUND TRUTH:** The athlete's category label is the absolute truth.
        2. **Metrics are Malleable:** Heart rate varies by day. 
        3. **Goal:** Do not judge. Instead, PROPOSE a new Heart Rate Map based on today's reality.

        **DATA INPUTS:**
        - **Baseline Map (The Theory):** {zones_text}
        - **Run Data (The Reality):** {perf_data}
        - **Context (Fatigue/Auxiliary):** {fatigue}

        **INSTRUCTIONS:**
        1. For each category run today, identify the **Actual Average HR**.
        2. Compare it to the Baseline Zone.
        3. If the difference is > 5 bpm, flag it as a **Significant Drift**.

        **OUTPUT FORMAT (Markdown):**
        ### 🧠 Run Analysis
        *(Brief observation)*
        ### 🗺️ Proposed Map (Today's Reality)
        | Effort Category | Baseline Zone | **Proposed Mapping** | Drift |
        | :--- | :--- | :--- | :--- |
        | [Category Name] | [e.g. 145-160] | **[Actual Avg HR]** | [e.g. +7 bpm 🚨] |
        ### 💡 Recommendation
        """

        run_date = run_ctx.get('date', 'today')
        # The word 'analyze' guarantees the Router sends this directly to the Coach!
        user_message = f"Please analyze my run from {run_date}."

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

            # We also enforce the date in your user prompt
            user_message = f"Please analyze my health condition for today ({today_str}), bearing in mind my recent workouts."

            return self.chat(
                user_input=user_message, 
                thread_id=thread_id, 
                system_context=system_instructions
            )