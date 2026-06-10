"""Claim-vs-action check: did the agent SAY it recorded without DOING it?

Deterministic enforcement layer for the coach's profile/cycle-config
writes (PROJECT_GUIDE §3.4.5). Motivating incident (2026-05-30, thread
coach_20260530T143250Z): the user answered every intake slot in one
message; the model replied "收到…我已将以下信息更新至你的档案…" with
ZERO tool calls — the recording was claimed, not performed. The slot
stayed empty and the coach re-asked the same questions for days.

The model can lie to the user, but not to an if-statement. After every
turn, `agentic_coach` runs:

    if claims_recording(answer) and not has_recording_call(tool_calls):
        → inject CORRECTION_PROMPT, run ONE more agent round
        → still lying? append WARNING_LINE (deterministic, visible)

Pure module: regex + list-walking, no I/O, no LLM. The regexes here
match the AGENT's own claim language — they never extract user facts
(that path stays LLM-driven + user-confirmed per project rules).

Design decisions, documented for the tests:

* FUTURE TENSE DOES NOT TRIGGER. "我会记录在案" / "收到后我会记录下来"
  are promises, not claims — the legitimate ask-then-record flow says
  exactly this while waiting for the user's answer. A claim window
  immediately preceded by 会/将/稍后/回头 etc. is skipped.
* DESCRIPTIVE READS DO NOT TRIGGER. "你的档案记录着X" (reading existing
  state) lacks the completed-write markers (已/记录在案/has been …) and
  stays out of the patterns.
* TRUE-BUT-STALE CLAIMS MAY TRIGGER. If the agent says "已记录在案"
  about a fact recorded in a PREVIOUS turn, this turn has no write call
  and the check fires. Accepted: the correction round either re-records
  (a harmless CME topic update — the two-threshold write dedups) or
  rephrases; one bounded extra round is the cost of never letting a
  false claim through unchallenged.
"""

from __future__ import annotations

import re

# The write tool this module polices. Prefetch entries and read tools
# (get_coach_profile / get_cycle_config / recall_topics) don't count —
# only an actual write does.
RECORDING_TOOL = "record_coach_fact"

# Sentinel prefixing the injected correction message. Doubles as the
# filter key: history endpoints drop human messages starting with this
# so the user never sees the system's correction prompt as "their" turn,
# and session consolidation skips it (it is not user speech).
SENTINEL = "[系统校验]"

CORRECTION_PROMPT = (
    f"{SENTINEL} 你在上一条回复中声称已经记录/更新了用户档案，但本轮没有任何 "
    f"`{RECORDING_TOOL}` 工具调用——声称的写入并未发生。现在二选一：\n"
    f"1. 如果该信息确实应该记录：立刻调用 `{RECORDING_TOOL}(area, raw_text, "
    f"conclusion)` 完成真实写入，然后用一两句话确认记录了什么；\n"
    f"2. 如果无法或不应记录：明确更正你的上一条回复，告诉用户信息尚未被记录。\n"
    f"不要再次声称已记录除非工具调用真实发生。"
)

WARNING_LINE = (
    "\n\n> ⚠️ 系统校验：本轮未发生档案写入"
    "（回复声称已记录，但没有 record_coach_fact 调用）。"
)

# Completed-write claim patterns. Chinese: anchored on perfective 已 /
# 记录在案 / 档案已更新 shapes. English: perfect-tense record/save/update
# aimed at the profile. Kept deliberately narrow — a missed lie costs one
# more user-visible incident; a false positive costs a bounded correction
# round, but a SLOPPY pattern (e.g. bare "记录") would fire on ordinary
# coaching prose constantly.
_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 已(经)(为你/帮你/替你)记录/存入/写入/保存…  — perfective write verb
    re.compile(r"已(?:经)?(?:为你|帮你|替你)?(?:记录|存入|写入|保存)"),
    # 已(经)将…更新/记录/存入/写入/保存 — the exact 5/30 shape
    re.compile(r"已(?:经)?将[^。\n]{0,24}?(?:更新|记录|存入|写入|保存)"),
    # 已(经)更新…档案/资料 — write verb then target
    re.compile(r"已(?:经)?更新[^。\n]{0,12}?(?:档案|资料)"),
    # 记录在案 (perfective by idiom; future guard handles 我会记录在案)
    re.compile(r"记录在案"),
    # 档案/资料已(经)更新
    re.compile(r"(?:档案|资料)已(?:经)?(?:更新|记录)"),
    # English perfect-tense variants
    re.compile(r"\bI(?:'ve| have)\s+(?:recorded|saved|updated|noted)\b", re.I),
    re.compile(r"\bhas been (?:recorded|saved|updated)\b", re.I),
    re.compile(r"\b(?:recorded|saved)\s+(?:to|in)\s+your\s+profile\b", re.I),
)

# Future/promise markers — if one of these sits immediately before the
# matched claim (within a few chars), it's a promise, not a claim.
_FUTURE_GUARD = re.compile(
    r"(?:会|将要|将|稍后|之后|回头|然后再?|待会儿?|等你?(?:回答|回复)后?|"
    r"\bwill\s*|\bgoing to\s*|\bI'll\s*)[^。.!?\n]{0,6}$"
)


def claims_recording(text: str) -> bool:
    """True if `text` claims a COMPLETED profile/记录 write."""
    if not text:
        return False
    for pat in _CLAIM_PATTERNS:
        for m in pat.finditer(text):
            # Look at a short window immediately before the match; a
            # future marker there ("我会记录在案") makes it a promise.
            window = text[max(0, m.start() - 12):m.start()]
            if _FUTURE_GUARD.search(window):
                continue
            return True
    return False


def _is_successful_write(tc: dict) -> bool:
    """A trace entry counts as a real write only if it's the write tool
    AND it did not error. ToolCallCaptureHandler stamps failed calls
    with an "error" key (on_tool_error path: invalid area → 400,
    transient MCP failure, …) and successful ones with "result" — a
    failed ATTEMPT must not suppress the claim check or light the badge
    (codex P2 on PR #105: claiming 已记录 after a failed write is still
    a false claim)."""
    return tc.get("name") == RECORDING_TOOL and "error" not in tc


def has_recording_call(tool_calls: list[dict]) -> bool:
    """True if the turn's collected tool calls include a SUCCESSFUL
    write-tool call.

    `tool_calls` is the trace sink: entries are dicts with at least
    {"name": ...}; prefetch entries carry prefetched=True but prefetch
    plans never include the write tool, so no special-casing needed.
    Errored write attempts (entries carrying "error") don't count.
    """
    return any(_is_successful_write(tc) for tc in tool_calls or [])


def recorded_areas(tool_calls: list[dict]) -> list[str]:
    """Areas SUCCESSFULLY written this turn, in call order (for the
    UI's ✓ badge). Errored attempts are excluded — same predicate as
    has_recording_call.

    Best-effort: each trace entry stores args either as a dict or a
    truncated repr string — extract `area` from both shapes; fall back
    to "?" so a write never disappears from the badge just because its
    args were truncated.
    """
    out: list[str] = []
    for tc in tool_calls or []:
        if not _is_successful_write(tc):
            continue
        args = tc.get("args")
        area = None
        if isinstance(args, dict):
            area = args.get("area")
        elif isinstance(args, str):
            m = re.search(r"['\"]area['\"]\s*:\s*['\"]([A-Za-z_.]+)['\"]", args)
            if m:
                area = m.group(1)
        out.append(area or "?")
    return out
