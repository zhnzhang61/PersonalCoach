from google import genai
import os
import streamlit as st
from dotenv import load_dotenv
from pathlib import Path
import json

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