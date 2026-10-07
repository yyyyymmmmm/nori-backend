#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes Agent 技能清单与开关；Hermes 是唯一数据源。

  1. 技能清单从运行中的 Hermes 容器 /home/agent/.hermes/skills 读取。
  2. 开关写入 Hermes config.yaml 的 skills.disabled：
       skills:
         disabled: []                  # 全局禁用 ← 本接口读写这个
         platform_disabled:
           telegram: [skill-a]         # 按平台禁用（v1 不碰，原样保留）
  3. 配置保存后重启 Hermes 容器使其生效。

App「智能体 → 技能」页的后端：
- GET  /api/hermes/skills → 每技能 id/name/description/enabled
- POST /api/hermes/skills → {skill_id, enabled} 改 disabled 列表并重启 Hermes。
"""
import re
import threading

import hermes_inspect

_lock = threading.Lock()

def _installed_skills():
    """Scan the skills installed in the live Hermes container."""
    ok, items = hermes_inspect.list_hermes_skills()
    if not ok:
        raise RuntimeError(str(items))
    return [(str(x.get("id") or ""), str(x.get("name") or x.get("id") or ""),
             str(x.get("description") or ""))
            for x in items if isinstance(x, dict) and x.get("id")]


def _read_disabled():
    """Read skills.disabled from the config Hermes itself is running with."""
    ok, config = hermes_inspect.get_hermes_config()
    if not ok or not isinstance(config, dict):
        raise RuntimeError("无法读取 Hermes 配置：%s" % config)
    skills = config.get("skills") or {}
    if not isinstance(skills, dict):
        return set()
    disabled = skills.get("disabled") or []
    if isinstance(disabled, str):
        disabled = [disabled]
    return {str(x) for x in disabled if str(x).strip()} if isinstance(disabled, list) else set()


def _write_disabled(disabled):
    """Patch skills.disabled in Hermes's live config, preserving all other fields."""
    ok, config = hermes_inspect.get_hermes_config()
    if not ok or not isinstance(config, dict):
        raise RuntimeError("无法读取 Hermes 配置：%s" % config)
    skills = dict(config.get("skills") or {})
    skills["disabled"] = sorted(disabled)
    ok, detail = hermes_inspect.update_hermes_config("skills", skills)
    if not ok:
        raise RuntimeError(detail)


def list_skills():
    """App 展示用：每技能 id/name/description/enabled（enabled=不在 disabled 列表）。"""
    disabled = _read_disabled()
    return [
        {"id": sid, "name": name, "description": desc, "enabled": sid not in disabled}
        for sid, name, desc in _installed_skills()
    ]


def set_skill(skill_id, enabled):
    """开关技能。返回 (ok: bool, error_zh: str, restarted: bool)。"""
    sid = str(skill_id or "").strip()
    if not sid or not re.fullmatch(r"[A-Za-z0-9_.-]+", sid):
        return False, "无效的技能 ID", False
    if not isinstance(enabled, bool):
        return False, "缺少 enabled 参数（true/false）", False
    with _lock:
        installed = {s[0] for s in _installed_skills()}
        if sid not in installed:
            return False, "技能不存在或未安装：%s" % sid, False
        disabled = _read_disabled()
        if enabled:
            disabled.discard(sid)
        else:
            disabled.add(sid)
        try:
            _write_disabled(disabled)
        except Exception as exc:  # noqa: BLE001
            return False, "写入 Hermes 配置失败：%s" % exc, False
        restarted, detail = hermes_inspect.restart_hermes_container()
        if not restarted:
            return True, "Hermes 容器重启失败：%s" % detail, False
        return True, "", True
