#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wave 3（P1）自测：通知偏好/频次闸、定时任务运行历史、记忆导入、数据导出。

跑法：cd backend && python3 wave3_test.py
只依赖标准库；不碰真实数据目录（STREAM_DATA_DIR / QL_DATA_DIR 指向临时目录）。
"""
import io
import json
import os
import stat
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_tmp = tempfile.mkdtemp(prefix="wave3_test_")
os.environ["STREAM_DATA_DIR"] = _tmp
os.environ["QL_DATA_DIR"] = _tmp
os.environ["QL_AUTO_LOGIN"] = "1"

import notify_prefs  # noqa: E402
import cron_runs  # noqa: E402
import memory_store  # noqa: E402
import memory_api  # noqa: E402
import data_api  # noqa: E402

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("PASS %s" % name)
    else:
        failed += 1
        print("FAIL %s" % name)


# ---------------------------------------------------------------------------
# 1. 通知偏好与频次闸
# ---------------------------------------------------------------------------
p = notify_prefs.get_prefs()
check("prefs 默认值齐全", p == {"daily_digest": True, "task_fail_notify": True,
                              "max_per_day": 5})

ok, res = notify_prefs.set_prefs({"max_per_day": 2, "daily_digest": False,
                                  "unknown_field": 1})
check("prefs 设置成功", ok and res["max_per_day"] == 2
      and res["daily_digest"] is False and "unknown_field" not in res)

ok, res = notify_prefs.set_prefs({"max_per_day": 9999})
check("prefs max_per_day 上限钳制", ok and res["max_per_day"] == 50)

ok, res = notify_prefs.set_prefs("not-a-dict")
check("prefs 非法参数拒绝", not ok)

st = os.stat(os.path.join(_tmp, "notify_prefs.json"))
check("prefs 文件 0600", stat.S_IMODE(st.st_mode) == 0o600)

# 频次闸：先恢复默认便于断言
notify_prefs.set_prefs({"daily_digest": True, "task_fail_notify": True,
                        "max_per_day": 2})
notify_prefs.reset_counters()
ok1, _ = notify_prefs.check_and_count("task_fail")
ok2, _ = notify_prefs.check_and_count("task_fail")
ok3, reason = notify_prefs.check_and_count("task_fail")
check("频次闸 2 次放行第 3 次拒绝", ok1 and ok2 and not ok3 and "上限" in reason)

ok1, _ = notify_prefs.check_and_count("digest")
ok2, reason = notify_prefs.check_and_count("digest")
check("摘要每天只允许 1 条", ok1 and not ok2)

notify_prefs.set_prefs({"task_fail_notify": False})
ok, reason = notify_prefs.check_and_count("task_fail")
check("关闭失败通知后闸门拒绝", not ok and "已关闭" in reason)
notify_prefs.set_prefs({"task_fail_notify": True, "max_per_day": 5})
notify_prefs.reset_counters()

# ---------------------------------------------------------------------------
# 2. 定时任务运行历史
# ---------------------------------------------------------------------------
ok, _ = cron_runs.record_run({"task_id": "t1", "task_name": "早报",
                              "finished_at": "2026-10-06T08:00:00",
                              "status": "success", "summary": "ok"})
check("record_run 成功", ok)
ok, err = cron_runs.record_run({"task_name": "缺 id"})
check("record_run 缺 task_id 拒绝", not ok and "task_id" in err)

cron_runs.record_run({"task_id": "t2", "finished_at": "2026-10-06T09:00:00",
                      "status": "failed", "error": "boom"})
runs = cron_runs.list_runs()
check("list_runs 倒序", len(runs) == 2 and runs[0]["task_id"] == "t2")
runs = cron_runs.list_runs(task_id="t1")
check("list_runs task_id 过滤", len(runs) == 1 and runs[0]["task_id"] == "t1")
runs = cron_runs.list_runs(limit=1)
check("list_runs limit", len(runs) == 1)

st = os.stat(os.path.join(_tmp, "cron_runs.jsonl"))
check("runs 文件 0600", stat.S_IMODE(st.st_mode) == 0o600)

# 去重：同 task_id+finished_at 重复上报只保留一条
cron_runs.record_run({"task_id": "t1", "finished_at": "2026-10-06T08:00:00",
                      "status": "success"})
check("重复上报去重", len(cron_runs.list_runs(task_id="t1")) == 1)

# 500 条裁剪（finished_at 唯一，避免被去重合并）；API 单页上限 200，存储保留 500
for i in range(505):
    cron_runs.record_run({"task_id": "bulk",
                          "finished_at": "2026-10-06T00:00:%05d" % i,
                          "status": "success"})
_lines = open(cron_runs._runs_path(), encoding="utf-8").read().strip().split("\n")
check("历史保留 500 条", len(_lines) == 500
      and len(cron_runs.list_runs(task_id="bulk", limit=500)) == 200)

# sync_from_hermes：mock _fetch_jobs + fake push_api
_fake_jobs = [
    {"id": "j1", "name": "晨报", "notify_on_fail": True,
     "latest_execution": {"started_at": "2026-10-06T07:00:00",
                          "finished_at": "2026-10-06T07:01:00",
                          "status": "failed", "error": "上游超时"}},
    {"id": "j2", "name": "晚报",
     "latest_execution": {"started_at": "2026-10-06T08:00:00",
                          "finished_at": "2026-10-06T08:01:00",
                          "status": "success"}},
    {"id": "j3", "name": "静默任务", "notify_on_fail": False,
     "latest_execution": {"started_at": "2026-10-06T09:00:00",
                          "finished_at": "2026-10-06T09:01:00",
                          "status": "failed", "error": "x"}},
    {"id": "j4", "name": "无执行"},
]
cron_runs._fetch_jobs = lambda: (_fake_jobs, "")
enqueued = []


class _FakePush:
    @staticmethod
    def enqueue(text):
        enqueued.append(text)
        return True, "已入队"


sys.modules["push_api"] = _FakePush
synced, notified, err = cron_runs.sync_from_hermes()
check("sync 同步 3 条有效执行", synced == 3 and not err)
check("失败通知只推 j1（j3 显式关闭）",
      notified == 1 and len(enqueued) == 1 and "晨报" in enqueued[0])
r = [x for x in cron_runs.list_runs(task_id="j1")][0]
check("已通知打标 notified=true", r.get("notified") is True)

# 再次同步：不重复通知
enqueued.clear()
synced2, notified2, _ = cron_runs.sync_from_hermes()
check("重复同步不重复通知", synced2 == 0 and notified2 == 0 and not enqueued)

# 未知状态不通知
cron_runs._fetch_jobs = lambda: ([{"id": "j5", "name": "怪任务",
                                   "latest_execution": {
                                       "finished_at": "2026-10-06T10:00:00",
                                       "status": "weird"}}], "")
enqueued.clear()
cron_runs.sync_from_hermes()
check("未知状态不打扰", not enqueued)

# ingest 口径
ok, payload = cron_runs.ingest({"task_id": "m1", "finished_at": "2026-10-06T11:00:00",
                                "status": "failed", "error": "手动触发失败"})
check("ingest 失败走通知", ok and payload.get("notified") is True)
ok, payload = cron_runs.ingest("not-a-dict")
check("ingest 非法参数拒绝", not ok)

# ---------------------------------------------------------------------------
# 3. 记忆导入
# ---------------------------------------------------------------------------
ex = memory_api._extract_memory_text
check("extract 纯字符串", ex("  hello world  ") == "hello world")
check("extract content", ex({"content": "记住我的生日"}) == "记住我的生日")
check("extract text", ex({"text": "喜欢喝茶"}) == "喜欢喝茶")
check("extract message", ex({"message": "住在上海"}) == "住在上海")
check("extract ChatGPT parts",
      ex({"content": {"parts": ["第一段", "第二段"]}}) == "第一段 第二段")
check("extract 列表", ex({"content": ["a", "b"]}) == "a b")
check("extract 非法形状", ex({"foo": 1}) == "" and ex(123) == "")

# 走完整导入循环（经 memory_store.add_entry 口径）
items = ["我喜欢喝咖啡",
         {"content": "我的狗叫旺财"},
         {"text": "每周三健身"},
         {"message": {"parts": ["住在", "北京"]}},  # message 下的 parts 也宽容解析
         "",
         "x",  # 太短 → 跳过
         "我喜欢喝咖啡"]  # 重复 → skipped
imported = skipped = 0
for it in items:
    t = memory_api._extract_memory_text(it)
    if not t or len(t) < 2:
        skipped += 1
        continue
    if memory_store.add_entry(t):
        imported += 1
    else:
        skipped += 1
check("导入 4 成功 3 跳过", imported == 4 and skipped == 3)
check("导入后条目在列表里", "我的狗叫旺财" in memory_store.list_entries()
      and "住在 北京" in memory_store.list_entries())

# ---------------------------------------------------------------------------
# 4. 数据导出
# ---------------------------------------------------------------------------
dirty = {"name": "ok", "api_key": "SECRET", "nested": {"Token": "T", "a": 1},
         "list": [{"password": "p", "b": 2}]}
clean = data_api._scrub(dirty)
s = json.dumps(clean)
check("scrub 丢弃密钥字段",
      clean == {"name": "ok", "nested": {"a": 1}, "list": [{"b": 2}]})
check("scrub 后无密钥字样",
      not any(h in s.lower() for h in ("secret", "api_key", "token", "password")))

payload = data_api.build_export()
check("export 字段齐全",
      all(k in payload for k in ("exported_at", "memory", "soul",
                                 "cron_tasks", "notify_prefs")))
check("export 含导入的记忆", "我的狗叫旺财" in payload["memory"])
check("export 通知偏好", payload["notify_prefs"]["max_per_day"] == 5)
es = json.dumps(payload).lower()
# 更强的断言：递归扫所有键名
def _all_keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield str(k)
            yield from _all_keys(v)
    elif isinstance(o, list):
        for x in o:
            yield from _all_keys(x)
bad = [k for k in _all_keys(payload)
       if any(h in k.lower() for h in ("key", "token", "secret", "password",
                                       "auth", "credential", "private"))]
check("export 键名无密钥残留", not bad)

zdata = data_api.build_zip(payload)
check("zip 非空", len(zdata) > 100)
zf = zipfile.ZipFile(io.BytesIO(zdata))
names = set(zf.namelist())
check("zip 文件齐全",
      names == {"memory.json", "soul.json", "cron_tasks.json",
                "notify_prefs.json", "README.txt"})
mem = json.loads(zf.read("memory.json").decode("utf-8"))
check("zip 内记忆可读", "我的狗叫旺财" in mem["entries"])
check("zip 内 README 安全声明", "不含任何 token" in
      zf.read("README.txt").decode("utf-8"))

print("\n%d/%d 通过" % (passed, passed + failed))
sys.exit(1 if failed else 0)
