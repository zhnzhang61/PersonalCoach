from google import genai
import os
import streamlit as st
from dotenv import load_dotenv
from pathlib import Path
import json

class AIAnalyst:
    def __init__(self, model="models/gemini-flash-latest"):
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
        
        # 1. Check Standard Environment
        key = os.getenv("GEMINI_KEY")
        if key: return key
        
        # 2. Force Load .env (Fix for VS Code terminal issues)
        # Look for .env in the same directory as this script
        current_dir = Path(__file__).resolve().parent
        env_path = current_dir / ".env"
        
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
            key = os.getenv("GEMINI_KEY")
            if key: return key
            
        # 3. Check Streamlit Secrets (Cloud Fallback)
        try:
            return st.secrets["GEMINI_KEY"]
        except:
            return None

    def analyze_run(self, context_dict):
        if not self.client:
            return "❌ Configuration Error: GEMINI_KEY not found. Please check your .env file."

        run_ctx = context_dict['run_context']
        
        # Format Data
        zones_text = "None Available"
        if run_ctx.get('hr_zones'):
            zones = run_ctx['hr_zones']
            # Handles both Custom (name/range) and Fallback formats
            zones_text = "\n".join([f"- {z['name']}: {z['range']}" for z in zones])

        perf_data = json.dumps(run_ctx.get('category_stats', []), indent=2)

        fatigue = "None reported."
        if context_dict.get('auxiliary_activities_last_7d'):
            fatigue = "\n".join([f"- {a['date']}: {a['type']} ({a['desc']})" for a in context_dict['auxiliary_activities_last_7d']])

        prompt = f"""
        ACT AS AN ELITE RUNNING COACH. 
        Analyze this workout based on the user's categorized lap data.

        **ATHLETE CONTEXT:**
        - Goal: {context_dict.get('block_goal', 'General Fitness')}
        - HR Zones: {zones_text}
        - Recent Fatigue: {fatigue}

        **WORKOUT EXECUTION:**
        - Date: {run_ctx.get('date')}
        {perf_data}

        **INSTRUCTIONS:**
        1. Compare the 'Avg HR' of each category against the athlete's HR Zones.
        2. Evaluate the pace consistency for 'Steady Effort' vs 'LT Effort' blocks.
        3. Provide a 'Verdict' (PASS/FAIL).
        4. Keep it concise, professional, and slightly witty.
        """
        
        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt
            )
            return response.text
        except Exception as e:
            return f"❌ AI Analysis Failed: {str(e)}"