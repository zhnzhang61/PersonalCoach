from google import genai
import os
import streamlit as st
from dotenv import load_dotenv
from pathlib import Path
import json
import pandas as pd

class AIAnalyst:
    def __init__(self, model="gemini-flash-latest"):
        self.model_id = model if model.startswith("models/") else f"models/{model}"
        self.api_key = self._find_api_key()
        
        try:
            if self.api_key:
                self.client = genai.Client(api_key=self.api_key)
            else:
                self.client = None
                print("⚠️ AI Analyst: No API Key found.")
        except Exception as e:
            self.client = None
            print(f"❌ SDK Init Error: {e}")

    def _find_api_key(self):
        """Hunts for the GEMINI_KEY in Environment, Local .env, or Streamlit Secrets."""
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

    def analyze_run(self, context_dict):
        if not self.client:
            return "❌ Configuration Error: GEMINI_KEY not found."

        run_ctx = context_dict['run_context']
        
        # 1. Format Baseline Zones
        zones_text = "None Available"
        if run_ctx.get('hr_zones'):
            zones = run_ctx['hr_zones']
            zones_text = "\n".join([f"- {z['name']}: {z['range']}" for z in zones])

        # 2. Format Execution Data (Grouped by Subjective Category)
        perf_data = json.dumps(run_ctx.get('category_stats', []), indent=2)

        # 3. Format Fatigue
        fatigue = "None reported."
        if context_dict.get('auxiliary_activities_last_7d'):
            fatigue = "\n".join([f"- {a['date']}: {a['type']} ({a['desc']})" for a in context_dict['auxiliary_activities_last_7d']])

        # --- THE RECALIBRATION PROMPT ---
        prompt = f"""
        ACT AS AN ELITE SPORTS DATA SCIENTIST.

        **THE PHILOSOPHY:**
        1. **Subjective Feel is GROUND TRUTH:** The athlete's category label (e.g., "Steady Effort") is the absolute truth.
        2. **Metrics are Malleable:** Heart rate varies by day (heat, fatigue, caffeine). 
        3. **Goal:** Do not judge. Instead, PROPOSE a new Heart Rate Map based on today's reality.

        **DATA INPUTS:**
        - **Baseline Map (The Theory):** {zones_text}
        
        - **Run Data (The Reality):**
        {perf_data}

        - **Context:** {fatigue}

        **INSTRUCTIONS:**
        1. For each category run today, identify the **Actual Average HR**.
        2. Compare it to the Baseline Zone.
        3. If the difference is > 5 bpm, flag it as a **Significant Drift**.

        **OUTPUT FORMAT (Markdown):**

        ### 🧠 Run Analysis
        *Brief, insightful observation of the run context (warmup issues, cardiac drift, or perfect execution).*

        ### 🗺️ Proposed Map (Today's Reality)
        *Based on your 'Ground Truth' feel, here is what your heart rate actually was:*

        | Effort Category | Baseline Zone | **Proposed Mapping** | Drift |
        | :--- | :--- | :--- | :--- |
        | [Category Name] | [e.g. 145-160] | **[Actual Avg HR]** | [e.g. +7 bpm 🚨] |

        *(Only include categories present in this run. Mark drifts > 5bpm with 🚨)*

        ### 💡 Recommendation
        *If a 🚨 alert was triggered: Is this a permanent fitness change (update your zones!) or temporary context (heat/fatigue/stress)?*
        """
        
        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt
            )
            return response.text
        except Exception as e:
            return f"❌ AI Analysis Failed: {str(e)}"

    def analyze_holistic_health(self, history_df, yesterday_raw):
        """
        Analyzes 14-day trends + yesterday's deep sleep data.
        """
        if not self.client: return "❌ No API Key."

        # 1. Summarize History
        trends = history_df.to_markdown()

        # 2. Extract Key Sleep Details from Raw JSON
        sleep_dto = yesterday_raw.get('dailySleepDTO', {})
        sleep_details = {
            "deep_sleep_min": sleep_dto.get('deepSleepSeconds', 0) / 60,
            "rem_sleep_min": sleep_dto.get('remSleepSeconds', 0) / 60,
            "awake_min": sleep_dto.get('awakeSleepSeconds', 0) / 60,
            "feedback": sleep_dto.get('sleepScoreFeedback'),
            "stress_during_sleep": yesterday_raw.get('avgSleepStress')
        }

        prompt = f"""
        ACT AS A HOLISTIC HEALTH & PERFORMANCE COACH.
        
        **OBJECTIVE:** Analyze the athlete's recovery status based on LONG-TERM TRENDS and YESTERDAY'S SLEEP.

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
        *Be specific: "Your RHR has crept up 3bpm since your long run on [Date]."*

        ### 🛌 Last Night's Quality
        *Don't just look at the score. Look at Deep vs. REM. Is the athlete physically recovered (Deep) but mentally tired (REM)?*

        ### 🚦 Readiness Verdict
        *Synthesize everything into a training recommendation.*
        *Options: [GREEN LIGHT: Push Hard], [YELLOW LIGHT: Aerobic Only], [RED LIGHT: Rest].*
        """

        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt
            )
            return response.text
        except Exception as e:
            return f"❌ Analysis Failed: {str(e)}"