#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动通知偏好与频次闸。

用户在 App 设置页控制主动推送：每日摘要开关、任务失败通知开关、
每日推送上限。所有主动推送先过频次闸，避免打扰用户。

偏好落盘 {STREAM_DATA_DIR}/notify_prefs.json（原子写，0600）。
计数器 {STREAM_DATA_DIR}/notify_counters.json（原子写，0600）：
{date: {kind: n}}，按自然日归零。

频次闸规则：
  - task_fail：prefs.task_fail_notify 须开；当日该 kind 计数 < max_per_day
  - digest：prefs.daily_digest 须开；当日 digest 计数 < 1（每天最多一条摘要）
只依赖标准库。
"""
import json
import os
import tempfile
import threading
import time

_lock = threading.Lock()

_DEFAULTS = {
    "daily_digest": True,
    "task_fail_notify": True,
    "max_per_day": 5,
}
_MAX_PER_DAY_LIMIT = 50  # 上限防刷


def _data_dir():
    return os.environ.get("STREAM_DATA_DIR", "/data/streams_data")


def _prefs_path():
    return os.path.join(_data_dir(), "notify_prefs.json")


def _counters_path():
    return os.path.join(_data_dir(), "notify_counters.json")


def _atomic_write_0600(path, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".notify.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def get_prefs():
    """返回偏好（与默认值合并，保证三字段齐全）。"""
    d = _DEFAULTS.copy()
    d.update(_read_json(_prefs_path()))
    return {
        "daily_digest": bool(d.get("daily_digest", True)),
        "task_fail_notify": bool(d.get("task_fail_notify", True)),
        "max_per_day": _clamp_max(d.get("max_per_day", 5)),
    }


def _clamp_max(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = _DEFAULTS["max_per_day"]
    return max(0, min(_MAX_PER_DAY_LIMIT, n))


def set_prefs(patch):
    """校验并保存偏好；返回 (ok, prefs_or_error)。未知字段忽略。"""
    if not isinstance(patch, dict):
        return False, "参数须为 JSON 对象"
    cur = get_prefs()
    if "daily_digest" in patch:
        cur["daily_digest"] = bool(patch["daily_digest"])
    if "task_fail_notify" in patch:
        cur["task_fail_notify"] = bool(patch["task_fail_notify"])
    if "max_per_day" in patch:
        cur["max_per_day"] = _clamp_max(patch["max_per_day"])
    try:
        with _lock:
            _atomic_write_0600(_prefs_path(), cur)
    except OSError as exc:
        return False, "保存失败：%s" % exc
    return True, cur


def _today():
    return time.strftime("%Y-%m-%d")


def _kind_limit(prefs, kind):
    if kind == "digest":
        return 1 if prefs["daily_digest"] else 0
    if kind == "task_fail":
        return prefs["max_per_day"] if prefs["task_fail_notify"] else 0
    return prefs["max_per_day"]


def check_and_count(kind):
    """频次闸：允许返回 (True, "")；拒绝返回 (False, 中文原因)。

    通过时把当日计数 +1（check 与 count 原子，避免并发超发）。
    kind: "task_fail" | "digest"（未知 kind 走通用上限）。
    """
    kind = str(kind or "task_fail")
    prefs = get_prefs()
    limit = _kind_limit(prefs, kind)
    if limit <= 0:
        if kind == "digest":
            return False, "每日摘要已关闭"
        if kind == "task_fail":
            return False, "任务失败通知已关闭"
        return False, "推送已关闭"
    today = _today()
    with _lock:
        counters = _read_json(_counters_path())
        day = counters.get(today, {})
        n = int(day.get(kind, 0) or 0)
        if n >= limit:
            return False, "已达今日推送上限（%d 条）" % limit
        day[kind] = n + 1
        counters[today] = day
        # 只保留最近 7 天计数，防文件膨胀
        old = sorted(counters.keys())
        for d in old[:-7]:
            counters.pop(d, None)
        try:
            _atomic_write_0600(_counters_path(), counters)
        except OSError:
            return False, "计数器写入失败"
    return True, ""


def reset_counters():
    """测试/运维用：清空计数器。"""
    with _lock:
        try:
            _atomic_write_0600(_counters_path(), {})
        except OSError:
            pass
