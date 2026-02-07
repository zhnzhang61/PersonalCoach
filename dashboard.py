# dashboard.py
import streamlit as st
import os
from garmin_sync import GarminDownloader
from data_processor import DataProcessor
from ai_analyst import HealthAnalyst

# Page Config
st.set_page_config(page_title="My Health Dashboard", page_icon="❤️")
st.title("My Health & Performance")

# Inputs for Credentials (in real use, use env vars)
with st.sidebar:
    st.header("Settings")
    email = st.text_input("Garmin Email")
    password = st.text_input("Garmin Password", type="password")
    gemini_key = st.text_input("Gemini API Key", type="password")
    fetch_btn = st.button("Sync Data")

# Initialize Logic
processor = DataProcessor()
sleep_text = "No data"
run_dist = 0.0

if fetch_btn and email and password:
    with st.spinner("Syncing with Garmin..."):
        downloader = GarminDownloader(email, password)
        if downloader.connect():
            # 1. Get Sleep
            raw_sleep = downloader.get_latest_sleep_data()
            sleep_seconds = processor.parse_sleep_json(raw_sleep)
            sleep_text = processor.format_duration(sleep_seconds)
            
            # 2. Get Run
            fit_path = downloader.download_latest_run_fit()
            if fit_path:
                run_dist = processor.decode_fit_file(fit_path)
            else:
                st.warning("No recent run found.")
        else:
            st.error("Login failed.")

# --- DISPLAY METRICS ---
col1, col2 = st.columns(2)

with col1:
    st.metric(label="Sleep Last Night", value=sleep_text)

with col2:
    # Convert meters to km
    km = round(run_dist / 1000, 2)
    st.metric(label="Latest Run Distance", value=f"{km} km")

# --- AI ANALYSIS ---
st.divider()
st.subheader("AI Performance Coach")

if gemini_key and (sleep_text != "No data" or run_dist > 0):
    analyst = HealthAnalyst(api_key=gemini_key, provider="gemini")
    
    context_data = {
        "sleep_duration": sleep_text,
        "recent_run_distance_meters": run_dist
    }
    
    if st.button("Analyze Performance"):
        with st.spinner("AI is analyzing your stats..."):
            insight = analyst.analyze(context_data)
            st.write(insight)