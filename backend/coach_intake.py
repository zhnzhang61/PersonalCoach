"""
Coach intake taxonomy — the questions a coach asks a new athlete.

Single source of truth for the "athlete profile (A) + per-cycle config (B)"
capture (PROJECT_GUIDE §3.4.5). Pure data + pure text rendering: no DB, no
LLM, no I/O. Three consumers:

  * cognitive_memory_engine.MemoryOS — record_coach_fact() validates `area`,
    and get_coach_profile()/get_cycle_config() iterate these lists to compute
    hard coverage (an area with no non-empty topic = a gap).
  * the agent prompt (PR-2) — splices in render_intake_prompt_section(), which
    is built FROM these slots so the good/vague standard lives in exactly one
    place (it also feeds the coverage judgment, never duplicated in the prompt).
  * UI — labels + filled/gap state.

Three natures of coaching question (PROJECT_GUIDE §3.4.5):
  A — static profile   : asked once, rarely changes  → PROFILE_SLOTS
  B — per-cycle config : re-asked each training cycle → CYCLE_SLOTS
  C — continuous       : re-sampled constantly        → NOT here; that is the
                          existing stream + models layer (data_processor).

Each `area` is fully qualified ("Profile.injury_history" / "Cycle.goal") and
is used verbatim as the topic's `root_category` in the CME, so the two
namespaces never collide with each other or with the free-text categories the
consolidation LLM emits ("Health/Recovery", "General", ...).

Slots within each list are ordered by coaching importance (top = ask first).

PROMPT-VERSION NOTE: good_example / vague_example are LLM-visible once PR-2
wires render_intake_prompt_section() into the system prompt. From that point
editing them is a prompt edit and falls under the §3.4.3 contract (bump
PROMPT_VERSION + changelog row). In PR-1 the render helper is dormant (nothing
imports it), so there is no behavior change yet.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoachSlot:
    """One intake question / coverage area."""

    area: str            # qualified key == topic.root_category, e.g. "Profile.injury_history"
    label: str           # short human label for UI + prompt
    question: str        # the intake question the coach would ask
    good_example: str    # ✅ a specific-enough answer (the standard to hit)
    vague_example: str   # ❌ a too-vague answer that should trigger a follow-up


# --------------------------------------------------------------------------
# A — static athlete profile (ask once; rarely changes), importance order
# --------------------------------------------------------------------------
PROFILE_SLOTS: tuple[CoachSlot, ...] = (
    CoachSlot(
        "Profile.injury_history",
        "伤病史",
        "过去有没有受过伤（应力性骨折、髂胫束、足底筋膜、跟腱等）、做过手术？"
        "现在恢复到什么程度，有没有反复发作的旧伤？",
        "2023 右胫骨应力性骨折，停跑 8 周后痊愈，无残留；无反复旧伤",
        "以前伤过，现在没事了",
    ),
    CoachSlot(
        "Profile.medical",
        "医疗 / 用药 / 最大心率",
        "有没有医疗状况（哮喘 / 心脏 / 贫血等）、正在服用什么药？"
        "知不知道自己的最大心率 / 静息心率？",
        "哮喘，运动前吸沙丁胺醇；无心脏 / 贫血；最大心率约 188、静息 48",
        "身体挺健康的",
    ),
    CoachSlot(
        "Profile.background",
        "跑步背景 / 训练年龄",
        "跑了几年了？跑过几个全马 / 半马，最好成绩分别是多少？",
        "跑步 6 年，完赛 3 个全马，PB 3:28；半马 PB 1:38",
        "跑了挺久了",
    ),
    CoachSlot(
        "Profile.demographics",
        "年龄 / 性别",
        "年龄、性别？",
        "34 岁，男",
        "三十多岁",
    ),
    CoachSlot(
        "Profile.gut_fueling",
        "肠胃 / 补给耐受",
        "长距离 / 比赛时肠胃耐不耐受？对咖啡因、能量胶这些补给的天然耐受如何？",
        "长跑每小时 60g 碳水（2 个胶）耐受良好，咖啡因没问题",
        "补给随便吃点",
    ),
    CoachSlot(
        "Profile.psychology",
        "心理 / 抗挫 / taper 反应",
        "比赛后程崩过、DNF 过吗？怎么应对硬课和挫折？"
        "以前减量（taper）时是觉得「腿没劲」还是「锋利」？",
        "30k 后撞过一次墙（配速掉 40s/km）；抗压一般，taper 时腿沉但不焦虑",
        "心态还行",
    ),
    CoachSlot(
        "Profile.coaching_prefs",
        "教练偏好 / 沟通",
        "希望我多严格 / 多频繁地反馈？偏数据驱动还是体感驱动？"
        "计划要细到每天，还是给个框架就行？",
        "要每天细化的计划，数据驱动，每周复盘一次",
        "你看着安排就行",
    ),
    CoachSlot(
        "Profile.devices",
        "设备",
        "用什么表 / 传感器？有没有心率带、功率计、跑步动态？数据信得过吗？",
        "Garmin Forerunner 965 + HRM-Pro 心率带，数据可信",
        "有块手表",
    ),
)


# --------------------------------------------------------------------------
# B — per-cycle config (re-asked each cycle; fixed within a cycle), importance order
# --------------------------------------------------------------------------
CYCLE_SLOTS: tuple[CoachSlot, ...] = (
    CoachSlot(
        "Cycle.goal",
        "目标 + 日期",
        "这个周期的目标是哪场比赛、什么时候、要什么成绩"
        "（完赛 / 破 X / BQ）？日期是硬目标还是软目标？",
        "Berlin 2026-09-21，目标 sub-3:30，日期是硬目标",
        "想跑个马拉松",
    ),
    CoachSlot(
        "Cycle.starting_volume",
        "当前训练量（起点）",
        "现在一周跑几次、周里程多少、近一个月最长一次多长、"
        "这个量稳定跑了多久了？",
        "周 40mi、5 次 / 周、最长 16mi、稳定 8 个月",
        "跑得还行",
    ),
    CoachSlot(
        "Cycle.blackout_dates",
        "不可动日期",
        "这个周期里有哪些绝对不能训练的日子"
        "（出差 / 休假 / 手术 / 理疗）？",
        "7/10–7/20 出差完全不能练；9/5 婚礼整天",
        "偶尔会忙",
    ),
    CoachSlot(
        "Cycle.weekly_availability",
        "每周可用天",
        "一周能稳定跑几天、哪些天固定哪些天灵活？长跑能放在哪天？",
        "每周 5 天，长跑固定周日，周三必休，其余灵活",
        "有空就跑",
    ),
    CoachSlot(
        "Cycle.session_time_caps",
        "单次时间上限",
        "工作日单次最多能练多久？周末呢？",
        "工作日单次 ≤ 60min，周末长跑可到 2.5h",
        "时间不太够",
    ),
    CoachSlot(
        "Cycle.quality_capacity",
        "质量课耐受",
        "哪些天可以吃硬课（之后需要恢复）？一周能扛几次质量课？",
        "一周能扛 2 次质量课（周二 / 周六），其余 easy",
        "能练几次硬的",
    ),
    CoachSlot(
        "Cycle.race_details",
        "目标赛事细节",
        "目标比赛的赛道剖面（平 / 丘陵 / 海拔）、预期气温、"
        "发枪时间、路面？",
        "柏林赛道平坦、9 月晨跑约 15°C、7:15 发枪、柏油路",
        "就一个普通马拉松",
    ),
    CoachSlot(
        "Cycle.life_load",
        "生活负荷",
        "这个周期里有没有可预见的大事——工作冲刺、搬家、添娃、"
        "考试、长差？",
        "8 月项目上线会加班 3 周；其余周期较稳",
        "工作有时候忙",
    ),
    CoachSlot(
        "Cycle.downweek_pref",
        "减量周偏好",
        "习惯每 3 周还是每 4 周一个减量周？对减量周什么反应？",
        "3:1 节奏（每 3 周一个 down week），减量减量不减强度",
        "累了就歇",
    ),
    CoachSlot(
        "Cycle.tuneup_races",
        "测试赛 / 热身赛",
        "愿不愿在周期中段跑个半马 / 10k 当 fitness 测试 + 比赛预演？什么时候？",
        "赛前 6 周跑一场半马当测试，7/26 本地半马",
        "也许会比个赛",
    ),
    CoachSlot(
        "Cycle.strength_crosstrain",
        "力量 / 交叉训练",
        "这个周期练不练力量 / 核心 / 灵活性？骑车 / 游泳 / 别的运动？一周几次？",
        "每周 2 次力量（周一 / 周五各 30min），无其它运动",
        "偶尔练练",
    ),
)


# Lookups -------------------------------------------------------------------
PROFILE_AREAS: frozenset[str] = frozenset(s.area for s in PROFILE_SLOTS)
CYCLE_AREAS: frozenset[str] = frozenset(s.area for s in CYCLE_SLOTS)
ALL_AREAS: frozenset[str] = PROFILE_AREAS | CYCLE_AREAS

# area -> CoachSlot, for O(1) label/question lookup.
SLOT_BY_AREA: dict[str, CoachSlot] = {
    s.area: s for s in (*PROFILE_SLOTS, *CYCLE_SLOTS)
}

# event_type stamped on the lossless episode (episodes.event_type is free
# TEXT, no CHECK — these two values just need to be stable + greppable).
PROFILE_EVENT_TYPE = "profile"
CYCLE_EVENT_TYPE = "cycle_config"


def event_type_for_area(area: str) -> str:
    """Map a qualified area to its episode event_type.

    Raises ValueError for an unknown area so callers fail loud rather than
    silently writing an episode under a bogus type.
    """
    if area in PROFILE_AREAS:
        return PROFILE_EVENT_TYPE
    if area in CYCLE_AREAS:
        return CYCLE_EVENT_TYPE
    raise ValueError(f"Unknown coach-intake area: {area!r}")


# --------------------------------------------------------------------------
# Prompt rendering — built FROM the slots so the good/vague standard lives in
# one place. DORMANT in PR-1 (no caller); PR-2 splices the output into the
# agent system prompt and bumps PROMPT_VERSION per §3.4.3.
# --------------------------------------------------------------------------
def _render_slots(slots: tuple[CoachSlot, ...]) -> str:
    lines: list[str] = []
    for s in slots:
        lines.append(f"- **{s.label}** — {s.question}")
        lines.append(f"  - ✅ {s.good_example}")
        lines.append(f"  - ❌ {s.vague_example}")
    return "\n".join(lines)


def render_intake_prompt_section() -> str:
    """The athlete-profile (A) + cycle-config (B) intake block for the agent
    system prompt (PROJECT_GUIDE §3.4.5). Pure text, derived from the slots.

    Behavior the block instructs:
      - read get_coach_profile + get_cycle_config (make_plan reads both;
        review_workout reads profile) before reasoning;
      - per task, judge which areas are REQUIRED and whether each is
        SPECIFIC ENOUGH (the ✅/❌ standard below);
      - a missing / vague REQUIRED area → ask ONE targeted follow-up and
        STOP (output only the question); record_coach_fact the answer only
        AFTER the user replies next turn — never an answer they haven't given;
      - non-critical gaps → don't interrupt; leave them for later.
    """
    return f"""## 运动员档案（A）与本周期配置（B）

出计划（make_plan）前**必读** `get_coach_profile` 和 `get_cycle_config`；\
复盘单次训练（review_workout）前读 `get_coach_profile`。

判断规则：
- 按当前任务判断哪些 area 是**必需**的，以及已有结论**够不够具体**。
- 必需但缺失 / 含糊 → **只问一个**最关键的针对性问题然后**停下**：这一轮\
只输出问题，先别出计划。用户**下一轮**回答后，先用 \
`record_coach_fact(area, raw_text)` 存下，再出计划。**绝不**用 \
`record_coach_fact` 记录用户还没给的答案（别替用户脑补回答）。
- 非必需的缺口 → 不要打断，留到以后再问。
- 已覆盖且具体 → 直接用，别重复问。

每个 area 的「够具体」标准（✅ 达标 / ❌ 太含糊、需追问）：

### A. 静态档案（问一遍就够）
{_render_slots(PROFILE_SLOTS)}

### B. 本周期配置（每个周期重设）
{_render_slots(CYCLE_SLOTS)}"""
