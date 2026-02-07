# ai_analyst.py
import google.generativeai as genai
# import openai  # Placeholder for ChatGPT

class HealthAnalyst:
    def __init__(self, api_key, provider="gemini"):
        self.provider = provider
        self.api_key = api_key

    def analyze(self, data_dict):
        prompt = (
            f"Act as a professional running coach. Analyze my recent health data: "
            f"I slept for {data_dict.get('sleep_duration')} and my most recent run was "
            f"{data_dict.get('recent_run_distance_meters', 0) / 1000:.2f} km. "
            "Give me a very brief assessment of my recovery and readiness."
        )

        if self.provider == "gemini":
            return self._ask_gemini(prompt)
        elif self.provider == "openai":
            return self._ask_chatgpt(prompt)
        else:
            return "Unknown provider."

    def _ask_gemini(self, prompt):
        try:
            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"Gemini API Error: {e}"

    def _ask_chatgpt(self, prompt):
        # Placeholder for ChatGPT implementation
        # client = openai.OpenAI(api_key=self.api_key)
        # response = client.chat.completions.create(...)
        return "ChatGPT module not yet enabled (Placeholder)."