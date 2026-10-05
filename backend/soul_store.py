#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Soul（AI 人设/输出风格）用户自定义存储。

App 设置页可配（GET/POST /api/soul），落盘 {STREAM_DATA_DIR}/soul.json
（原子写，0600），stream_api 经 _soul_prompt() 动态读取，免重启生效。
未设置自定义时返回 None，由调用方回退到内置默认 SOUL_PROMPT。
只依赖标准库。
"""
import json
import os
import tempfile

_CONFIG_NAME = "soul.json"
_KEY = "soul"


def _data_dir():
    return os.environ.get("STREAM_DATA_DIR", "/data/streams_data")


def _config_path():
    return os.path.join(_data_dir(), _CONFIG_NAME)


def _load():
    try:
        with open(_config_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def get_custom():
    """返回用户自定义 soul 文本；未设置返回 None。"""
    v = _load().get(_KEY)
    if isinstance(v, str) and v.strip():
        return v
    return None


def get_soul(default):
    """自定义优先，未设置则用内置默认。"""
    c = get_custom()
    return c if c is not None else default


def save_custom(text):
    """保存自定义 soul；空字符串 = 恢复默认（删键）。返回 (ok: bool, error_zh: str)。"""
    text = text if isinstance(text, str) else ""
    d = _load()
    if text.strip():
        if len(text) > 20000:
            return False, "人设文本过长（最多 20000 字）"
        d[_KEY] = text
    else:
        d.pop(_KEY, None)
    data_dir = _data_dir()
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as e:
        return False, "无法创建数据目录：%s" % e
    path = _config_path()
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".soul.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        return False, "保存失败：%s" % e
    return True, ""
