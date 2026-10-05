#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时任务运行历史。

架构现实：Hermes 实际执行定时任务，本后端是代理层（cron_api 透传
Hermes /api/jobs），没有执行回调可挂。因此运行历史 = 本地 jsonl 镜像：
每次查询 GET /api/cron/runs 时先从 Hermes /api/jobs 的 latest_execution
同步（upsert，去重键 task_id + finished_at），再返回本地历史。
另提供 ingest() 供外部执行器显式上报一条运行记录。

记录字段：task_id / task_name / started_at / finished_at / status /
summary / error / notified（是否已就失败推送过通知）。
落盘 {STREAM_DATA_DIR}/cron_runs.jsonl（0600，保留最近 500 条）。

失败通知：status 命中失败集合、且任务配置未显式关闭
（job 字典里 notify_on_fail != False）、且 notify_prefs 频次闸放行、
且本条未通知过 → 经 push_api.enqueue 入队，并把 notified=true 写回，
避免重复打扰。未知状态不通知（宁可漏，不可扰）。
只依赖标准库。
"""
import json
import os
import tempfile
import threading
import time
import urllib.request

import hermes_upstream
import notify_prefs

_lock = threading.Lock()

_FILE_NAME = "cron_runs.jsonl"
_KEEP = 500

_FAIL_STATUSES = {"failed", "error", "failure", "timeout", "timed_out",
                  "cancelled", "canceled"}
_OK_STATUSES = {"success", "succeeded", "ok", "completed", "done"}


def _data_dir():
    return os.environ.get("STREAM_DATA_DIR", "/data/streams_data")


def _runs_path():
    return os.path.join(_data_dir(), _FILE_NAME)


def _read_all():
    """读全部记录（调用方自己加锁或在锁内调用本函数时注意：本函数不加锁）。"""
    recs = []
    try:
        with open(_runs_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("task_id"):
                    recs.append(d)
    except OSError:
        pass
    return recs


def _write_all(recs):
    data_dir = _data_dir()
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".cronruns.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in recs[-_KEEP:]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, _runs_path())
    except OSError:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def _key(rec):
    return (str(rec.get("task_id", "")), str(rec.get("finished_at", "")))


def record_run(rec):
    """写入一条运行记录；返回 (ok, error_zh)。"""
    if not isinstance(rec, dict) or not str(rec.get("task_id", "")).strip():
        return False, "task_id 不能为空"
    clean = {
        "task_id": str(rec.get("task_id", ""))[:128],
        "task_name": str(rec.get("task_name", "") or "")[:200],
        "started_at": str(rec.get("started_at", "") or "")[:64],
        "finished_at": str(rec.get("finished_at", "") or "")[:64],
        "status": str(rec.get("status", "unknown") or "unknown")[:32].lower(),
        "summary": str(rec.get("summary", "") or "")[:500],
        "error": str(rec.get("error", "") or "")[:1000],
        "notified": bool(rec.get("notified", False)),
        "recorded_at": int(time.time()),
    }
    try:
        with _lock:
            recs = _read_all()
            # 同一 task_id+finished_at 去重（重复上报只保留最新）
            k = _key(clean)
            recs = [r for r in recs if _key(r) != k]
            recs.append(clean)
            _write_all(recs)
    except OSError as exc:
        return False, "写入失败：%s" % exc
    return True, ""


def list_runs(task_id=None, limit=50):
    """按时间倒序返回历史；task_id 过滤；limit 上限 200。"""
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    with _lock:
        recs = _read_all()
    if task_id:
        tid = str(task_id)
        recs = [r for r in recs if str(r.get("task_id")) == tid]
    recs.sort(key=lambda r: (str(r.get("finished_at", "")),
                             int(r.get("recorded_at", 0) or 0)), reverse=True)
    return recs[:limit]


def mark_notified(task_id, finished_at):
    """把某条记录标为已通知（防重复打扰）。"""
    k = (str(task_id), str(finished_at))
    try:
        with _lock:
            recs = _read_all()
            changed = False
            for r in recs:
                if _key(r) == k and not r.get("notified"):
                    r["notified"] = True
                    changed = True
            if changed:
                _write_all(recs)
    except OSError:
        pass
    return changed


def _is_fail(status):
    return str(status or "").lower() in _FAIL_STATUSES


def _fetch_jobs():
    """从 Hermes 拉任务列表（含 latest_execution）；失败返回 ([], error_zh)。"""
    try:
        req = urllib.request.Request(
            "%s/api/jobs?include_disabled=true" % hermes_upstream.get_base_url(),
            headers={"Authorization": "Bearer %s" % hermes_upstream.get_key()})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        jobs = data.get("jobs", [])
        if not isinstance(jobs, list):
            return [], "Hermes 返回格式异常"
        return jobs, ""
    except Exception as exc:  # noqa: BLE001
        return [], "连接 Hermes 失败：%s" % exc


def _job_allows_fail_notify(job):
    """任务配置是否允许失败通知：显式 False 才关，缺省开。"""
    if not isinstance(job, dict):
        return True
    v = job.get("notify_on_fail", job.get("notify", None))
    return v is not False


def _maybe_notify_fail(rec, job):
    """失败通知：频次闸 + 去重。返回 True 表示已入队。"""
    if not _is_fail(rec.get("status")):
        return False
    if not _job_allows_fail_notify(job):
        return False
    if rec.get("notified"):
        return False
    ok, _reason = notify_prefs.check_and_count("task_fail")
    if not ok:
        return False
    try:
        import push_api
    except Exception:  # noqa: BLE001
        return False
    name = rec.get("task_name") or rec.get("task_id")
    err = rec.get("error") or rec.get("summary") or "未知错误"
    text = "⏰ 定时任务「%s」执行失败：%s" % (name, err[:200])
    pushed, _msg = push_api.enqueue(text)
    if pushed:
        mark_notified(rec.get("task_id"), rec.get("finished_at"))
        return True
    return False


def sync_from_hermes():
    """从 Hermes 同步最新执行记录到本地；触发失败通知。

    返回 (synced_n, notified_n, error_zh)。Hermes 不可达时 error_zh 非空，
    此时仍返回本地已有历史（调用方决定）。
    """
    jobs, err = _fetch_jobs()
    if err:
        return 0, 0, err
    synced = 0
    notified = 0
    for job in jobs:
        exec_data = job.get("latest_execution") if isinstance(job, dict) else None
        if not isinstance(exec_data, dict):
            continue
        finished = str(exec_data.get("finished_at", "") or "")
        if not finished:
            continue
        rec = {
            "task_id": str(job.get("id", "")),
            "task_name": str(job.get("name", "") or ""),
            "started_at": str(exec_data.get("started_at", "") or ""),
            "finished_at": finished,
            "status": str(exec_data.get("status", "unknown") or "unknown"),
            "summary": str(exec_data.get("summary", "") or "")[:500],
            "error": str(exec_data.get("error", "") or "")[:1000],
        }
        # 去重：已存在（同 task_id+finished_at）则跳过
        with _lock:
            exists = any(_key(r) == (rec["task_id"], finished)
                         for r in _read_all())
        if exists:
            continue
        ok, _e = record_run(rec)
        if ok:
            synced += 1
            if _maybe_notify_fail(rec, job):
                notified += 1
    return synced, notified, ""


def ingest(body):
    """外部执行器显式上报：POST /api/cron/runs 的 body 口径。

    返回 (ok: bool, payload: dict)。失败状态同样走失败通知链路。
    """
    if not isinstance(body, dict):
        return False, {"error": "参数须为 JSON 对象"}
    ok, err = record_run(body)
    if not ok:
        return False, {"error": err}
    rec = {
        "task_id": str(body.get("task_id", "")),
        "task_name": str(body.get("task_name", "") or ""),
        "finished_at": str(body.get("finished_at", "") or ""),
        "status": str(body.get("status", "unknown") or "unknown"),
        "summary": str(body.get("summary", "") or ""),
        "error": str(body.get("error", "") or ""),
    }
    notified = _maybe_notify_fail(rec, {})
    return True, {"ok": True, "notified": notified}
