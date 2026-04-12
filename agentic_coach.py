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
    # --- STEP 1: 引入 Semantic Memory ---
    def __init__(self, db_path="data/chat_memory.db", user_profile: dict = None):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        # 接收全局档案，如果没有传入则给个空的
        self.user_profile = user_profile or {}
        
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

    # --- STEP 1 延续: 将全局记忆焊入 System Prompt ---
    def _coach_node(self, state: State):
        profile_str = json.dumps(self.user_profile, indent=2)
        sys_msg = SystemMessage(content=f"""
        You are an elite Running Coach and Sports Physiologist. 
        Focus on biomechanics, pace, splits, and training, but ALWAYS connect them to the athlete's overall health context.
        
        === USER BASELINE PROFILE (SEMANTIC MEMORY) ===
        {profile_str}
        ===============================================
        Use this baseline to evaluate all incoming data.
        """)
        messages = [sys_msg] + state["messages"]
        response = self.llm.invoke(messages)
        return {"messages": [response]}

    def _doctor_node(self, state: State):
        profile_str = json.dumps(self.user_profile, indent=2)
        sys_msg = SystemMessage(content=f"""
        You are an elite physiological Health Doctor. 
        Focus on HRV, Sleep Scores, and nervous system recovery. 
        
        === USER BASELINE PROFILE (SEMANTIC MEMORY) ===
        {profile_str}
        ===============================================
        Acknowledge running data if it explains fatigue, but stick to your domain.
        """)
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
        return self.chat(user_input=user_input, thread_id=thread_id, system_context=None)

    def summarize_thread(self, thread_id: str):
        """
        读取特定 thread 的聊天记录，并总结出核心建议，准备写入永久记忆。
        """
        history = self.get_history(thread_id)
        # 如果只有一条系统提示和一句用户的话，说明没怎么深聊，直接跳过
        if len(history) <= 3: 
            return None 
            
        chat_text = "\n".join([f"{msg.type}: {msg.content}" for msg in history if msg.type in ['human', 'ai']])
        
        prompt = f"""
        请将以下教练与运动员的对话，压缩成1-2句话的核心结论或建议。
        重点提取：运动员的痛点/感受，以及教练给出的具体对策。使用第三人称陈述句。
        
        对话记录：
        {chat_text}
        """
        # 使用 router_llm (Temperature=0) 保证总结客观精准
        response = self.router_llm.invoke([HumanMessage(content=prompt)])
        return response.content.strip()

    # --- STEP 2: Working Memory 融合分析 ---
    # --- STEP 2: Working Memory 融合分析 ---
    def analyze_run(self, working_memory_dict: dict, thread_id: str, telemetry_df=None, historical_memories: list = None):
        """
        Takes the dense JSON from dp.build_agent_working_memory() and historical records to perform a deep analysis.
        """
        run_name = working_memory_dict.get('workout_summary', {}).get('name', '未命名训练')
        
        # 提取历史相似记录 (如果有的话)
        history_section = ""
        if historical_memories:
            history_section = "\n\n**历史背景 (过去的相似训练):**\n"
            for mem in historical_memories:
                history_section += f"- {mem['date']}: {mem['summary']}\n"

        telemetry_section = ""
        if telemetry_df is not None and not telemetry_df.empty:
            csv_data = telemetry_df.to_csv(index=False)
            telemetry_section = f"\n\n**原始遥测数据 (逐圈数据):**\n```csv\n{csv_data}\n```"

        working_memory_str = json.dumps(working_memory_dict, indent=2)

        system_instructions = f"""
        请扮演一名顶尖的运动数据科学家和生理学家。

        **数据输入:**
        - **今日工作记忆 (健康 + 训练上下文):**
        ```json
        {working_memory_str}
        ```
        {history_section}
        {telemetry_section}

        **分析指令:**
        1. **分析意图与执行:** 使用工作记忆 JSON 中的数据作为你的分析框架。将 `daily_readiness` (每日准备度/睡眠/HRV等) 指标与其实际的跑步表现直接联系起来。
        2. **历史对比:** 如果提供了“历史背景”，请明确将今天的跑步数据与过去的表现进行对比，以指出他的进步或是反复出现的问题。
        3. **遥测数据分析 (关键):**
           - 评估被标记圈数 (Laps) 的曲线形状 (配速、步频、海拔)。
           - 识别速度/间歇跑中的心脏滞后效应 (Cardiac Lag)。将实际/峰值心率与基准档案 (Baseline Profile) 里的预期心率区间进行比对。

        **输出格式 (使用 Markdown):**
        ### 🧠 训练分析: {run_name}
        *(详细的观察报告：将当天的生理准备度与实际执行的遥测数据联系起来。如果适用，请与历史记录进行对比)。*
        
        ### 🗺️ 心率区间映射 (今日实际情况)
        | 努力等级 (Category) | 基准区间 (Baseline) | **实际映射 (Actual/Peak HR)** | 心率漂移 (Drift) |
        | :--- | :--- | :--- | :--- |
        | [例如：马拉松配速] | [例如：145-160] | **[填入实际或峰值心率]** | [例如：+7 bpm 🚨] |
        
        ### 💡 建议
        *(给出下一步可执行的生理学或训练建议)。*
        """

        run_date = working_memory_dict.get('date', '今天')
        user_message = f"请分析我在 {run_date} 进行的名为 '{run_name}' 的训练执行情况。"

        return self.chat(
            user_input=user_message, 
            thread_id=thread_id, 
            system_context=system_instructions
        )

    # --- STEP 3: Episodic Memory (生成供未来 RAG 使用的短记忆) ---
    def generate_episodic_summary(self, working_memory_dict: dict, telemetry_df=None):
        """
        Calls the LLM purely to generate a dense, factual summary and tags to be saved 
        into the episodic vector/JSON database.
        """
        run_name = working_memory_dict.get('workout_summary', {}).get('name', 'Unnamed Workout')
        working_memory_str = json.dumps(working_memory_dict, indent=2)
        
        prompt = f"""
        You are an AI Memory Summarizer. Look at this run and compress the core physiological takeaways into a dense 50-75 word summary. 
        Focus on facts: Distance, Pace, HR Drift, and how Daily Readiness (like sleep) affected it. 
        Also, assign 2-4 broad categorization tags (e.g., "Long Run", "Fatigue", "VO2Max", "Hot Weather").

        Context:
        ```json
        {working_memory_str}
        ```

        Output EXACTLY in this JSON format, nothing else:
        {{
            "tags": ["Tag1", "Tag2"],
            "summary_text": "Your dense summary here."
        }}
        """
        # Create a temporary stateless LLM call for formatting
        formatting_llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.1, api_key=self.api_key)
        response = formatting_llm.invoke([SystemMessage(content="You return strictly JSON."), HumanMessage(content=prompt)])
        
        try:
            # Clean up potential markdown formatting from the response
            content = response.content.replace('```json', '').replace('```', '').strip()
            return json.loads(content)
        except Exception as e:
            print(f"Error generating episodic memory: {e}")
            return {"tags": ["Analysis"], "summary_text": f"Completed {run_name}."}

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
        请扮演一名全科健康与运动表现医生 (Holistic Health & Performance Doctor)。
        
        **核心目标:** 根据长期数据趋势和昨晚的睡眠质量 ({yesterday_str})，深度分析运动员今天 ({today_str}) 的身体恢复状态。

        **数据源 1: 过去 14 天的历史趋势 (CSV 格式)**
        {trends}
        *(列名说明: sleep_score=睡眠分数, rhr=静息心率, hrv=心率变异性, run_miles=跑步里程, stress=全天压力值)*

        **数据源 2: 昨夜睡眠深度解析 (JSON 提取)**
        - 深度睡眠 (Deep Sleep): {sleep_details['deep_sleep_min']:.0f} 分钟
        - 快速眼动睡眠 (REM Sleep): {sleep_details['rem_sleep_min']:.0f} 分钟
        - 清醒/焦躁时间 (Awake/Restless): {sleep_details['awake_min']:.0f} 分钟
        - 佳明官方反馈 (Garmin Feedback): "{sleep_details['feedback']}"
        - 睡眠期间压力值 (Overnight Stress): {sleep_details['stress_during_sleep']} (该数值越低越好)

        **分析要求 (请务必使用 Markdown 格式输出中文报告):**
        
        ### 📉 趋势诊断
        *查看 14 天的历史数据。静息心率 (RHR) 是否在上升？心率变异性 (HRV) 是否正在走低？睡眠分数与近期的跑步里程之间有什么相关性？*
        *请给出非常具体的医学/生理学观察。*

        ### 🛌 昨夜睡眠质量
        *不要只读总分。对比深度睡眠 (修复身体) 与快速眼动睡眠 (修复神经/精神) 的比例。分析一下该运动员昨晚是真的获得了充分休息，还是处于“身体恢复了但精神依然疲惫”的状态？结合睡眠压力值进行点评。*

        ### 🚦 今日状态裁决
        *综合以上所有客观数据，为今天 ({today_str}) 给出明确的训练建议。*
        *请从以下三个选项中选择一个作为基调，并给出理由：*
        * [🟢 绿灯：状态极佳，可以上高强度训练]
        * [🟡 黄灯：恢复中，建议仅限轻松有氧或交叉训练]
        * [🔴 红灯：严重疲劳或有生病风险，建议彻底休息]
        """

        # 这里修改了隐形触发词，确保精准路由给 Doctor 处理健康问题
        user_message = f"请结合我近期的生理指标，深度分析一下我今天 ({today_str}) 的身体健康、恢复状态和睡眠质量。"

        return self.chat(
            user_input=user_message, 
            thread_id=thread_id, 
            system_context=system_instructions
        )