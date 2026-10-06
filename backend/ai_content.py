#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 内容生成 API：点子、今日建议。提示词在后端统一管理，iOS 只负责展示。

端点（挂在 agent_api 或 hermes_api 下）：
  GET /api/agent/ideas       → {ideas: [{icon,title,desc,prompt,group}], fallback: bool}
  GET /api/agent/suggestions → {suggestions: [{title,reason,prompt}], fallback: bool}

逻辑：调 Hermes 生成；失败返回内置模板（fallback=true，iOS 诚实标注）。
"""
import json
import time

# 复用既有 oneShot 通道（经 Hermes）
import os
import urllib.request

def _call_ai(prompt, timeout=60):
    """调 Hermes 生成。返回文本或 None。"""
    try:
        import hermes_upstream
        url = hermes_upstream.chat_completions_url()
        key = hermes_upstream.get_key()
        if not url:
            return None
        body = json.dumps({
            "model": hermes_upstream.effective_model() or "default",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2000,
        }).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + (key or ""),
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        # OpenAI 格式
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            return msg.get("content")
        return None
    except Exception:
        return None


_IDEAS_CACHE = {"ts": 0, "date": "", "data": []}
_SUGGEST_CACHE = {"ts": 0, "date": "", "data": []}
_CACHE_TTL = 3600  # 1 小时


def _today():
    return time.strftime("%Y-%m-%d")


def _parse_json_array(raw):
    """从文本中提取 JSON 数组。"""
    if not raw:
        return None
    try:
        s = raw.index("[")
        e = raw.rindex("]")
        if s >= e:
            return None
        return json.loads(raw[s:e + 1])
    except Exception:
        return None


_IDEAS_PROMPT = """你是Nori的生活助手。今天是%s。请为用户生成 4-6 条个性化推荐（点子）：每条都是你现在就能帮用户做的具体事项，要实用、具体，贴合一天中的这个时间点。
每条推荐包含：icon（SF Symbol 名）、title（简短有力的标题）、desc（2-3 句话，详细说明你会怎么做、需要什么信息、产出什么）、prompt（用户点开后填入对话框的完整提示词，要详细全面、可直接使用）、group（分组名，从"今日效率""规划复盘""生活助手"中选一个）。
只返回 JSON 数组，不要任何其他文字。格式示例：
[{"icon":"calendar","title":"今日会议准备","desc":"……","prompt":"……","group":"今日效率"}]
icon 只能从这些里面选：list.bullet.clipboard,envelope,envelope.open,calendar,chart.line.uptrend.xyaxis,alarm,lightbulb,sparkles,bell,checkmark.circle
"""

_IDEAS_FALLBACK = [
    {"icon": "lightbulb", "title": "今日待办整理", "desc": "把今天要做的事列出来，按优先级排序",
     "prompt": "帮我整理今天的待办事项", "group": "今日效率"},
    {"icon": "calendar", "title": "日程回顾", "desc": "回顾今天的日程安排",
     "prompt": "帮我回顾今天的日程", "group": "规划复盘"},
]

_SUGGEST_PROMPT = """你是Nori的生活助手。现在是%s%s。请给出3条今日建议，每条都是具体的建议陈述句，不是提问。
要求：title（建议标题，10字内，如"下午带伞"）；reason（依据，1句话，如"天气预报下午有雨"）；prompt（用户点击后填入对话框的完整提示词，要具体可执行）。
只返回JSON数组：[{"title":"...","reason":"...","prompt":"..."}]
示例：{"title":"今晚早点休息","reason":"你连续3天睡眠不足7小时","prompt":"帮我制定一个今晚的作息计划，保证23点前入睡"}
"""


def _time_desc():
    h = int(time.strftime("%H"))
    if 5 <= h < 9:
        return "清晨"
    if 9 <= h < 12:
        return "上午"
    if 12 <= h < 14:
        return "中午"
    if 14 <= h < 18:
        return "下午"
    if 18 <= h < 23:
        return "晚上"
    return "深夜"


def get_ideas(force=False):
    """获取点子列表。"""
    today = _today()
    c = _IDEAS_CACHE
    if not force and c["date"] == today and c["data"]:
        return {"ideas": c["data"], "fallback": False}
    date_str = time.strftime("%m月%d日 %A")
    raw = _call_ai(_IDEAS_PROMPT % date_str)
    arr = _parse_json_array(raw)
    valid = []
    if arr:
        for d in arr:
            if isinstance(d, dict) and d.get("title") and d.get("prompt"):
                valid.append({
                    "icon": str(d.get("icon") or "lightbulb"),
                    "title": str(d["title"]),
                    "desc": str(d.get("desc") or ""),
                    "prompt": str(d["prompt"]),
                    "group": str(d.get("group") or ""),
                })
    if valid:
        c.update({"ts": time.time(), "date": today, "data": valid})
        return {"ideas": valid, "fallback": False}
    return {"ideas": _IDEAS_FALLBACK, "fallback": True}


def get_suggestions(force=False):
    """获取今日建议。"""
    today = _today()
    c = _SUGGEST_CACHE
    if not force and c["date"] == today and c["data"]:
        return {"suggestions": c["data"], "fallback": False}
    date_str = time.strftime("%m月%d日 %A")
    raw = _call_ai(_SUGGEST_PROMPT % (date_str, _time_desc()), timeout=30)
    arr = _parse_json_array(raw)
    valid = []
    if arr:
        for d in arr:
            if isinstance(d, dict) and d.get("title") and d.get("prompt"):
                valid.append({
                    "title": str(d["title"]),
                    "reason": str(d.get("reason") or ""),
                    "prompt": str(d["prompt"]),
                })
    if valid:
        c.update({"ts": time.time(), "date": today, "data": valid})
        return {"suggestions": valid, "fallback": False}
    return {"suggestions": [
        {"title": "规划今天", "reason": "通用建议", "prompt": "帮我规划一下今天的日程"},
        {"title": "健康提醒", "reason": "通用建议", "prompt": "提醒我今天的健康目标"},
    ], "fallback": True}
