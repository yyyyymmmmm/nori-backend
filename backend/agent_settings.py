#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用设置存储：上下文压缩等开关存后端，换设备一致。
端点：
  GET  /api/agent/settings        → {settings: {...}}
  POST /api/agent/settings        → body: {key: value} → {ok: true}
存储：QL_DATA_DIR/agent_settings.json
"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("QL_DATA_DIR", os.path.join(os.path.dirname(BASE), "data"))
SETTINGS_FILE = os.path.join(DATA_DIR, "agent_settings.json")

DEFAULTS = {
    "context_auto_compress": True,
    "context_threshold": 8000,
}

_ALLOWED = set(DEFAULTS)


def get_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
            # 合并默认值
            out = dict(DEFAULTS)
            out.update(d)
            return out
    except Exception:
        return dict(DEFAULTS)


def set_setting(key, value):
    if key not in _ALLOWED:
        return False
    if key == "context_auto_compress" and not isinstance(value, bool):
        return False
    if key == "context_threshold":
        if isinstance(value, bool) or not isinstance(value, int) or not 1000 <= value <= 64000:
            return False
    try:
        settings = get_settings()
        settings[key] = value
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETTINGS_FILE)
        return True
    except Exception:
        return False
