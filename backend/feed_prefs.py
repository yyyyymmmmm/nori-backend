#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feed 偏好存储：prompt 存后端，换设备也一致。
端点：
  GET  /api/agent/feed/prompt  → {prompt: str}
  POST /api/agent/feed/prompt  → {prompt: str} body: {prompt: str}
存储：QL_DATA_DIR/feed_prompt.json
"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("QL_DATA_DIR", os.path.join(os.path.dirname(BASE), "data"))
PROMPT_FILE = os.path.join(DATA_DIR, "feed_prompt.json")

DEFAULT_PROMPT = "科技、AI、效率工具"


def get_prompt():
    try:
        with open(PROMPT_FILE, encoding="utf-8") as f:
            d = json.load(f)
            p = d.get("prompt", "")
            return p if p else DEFAULT_PROMPT
    except Exception:
        return DEFAULT_PROMPT


def set_prompt(prompt):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = PROMPT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"prompt": prompt}, f, ensure_ascii=False)
        os.replace(tmp, PROMPT_FILE)
        return True
    except Exception:
        return False
