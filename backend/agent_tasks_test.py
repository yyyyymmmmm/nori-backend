#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_tasks 自测：创建/查询/取消/通知入队/落盘/重启恢复/HTTP 全链路。"""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import urllib.request

TMP = tempfile.mkdtemp(prefix="agent_tasks_test.")
os.environ["STREAM_DATA_DIR"] = TMP
os.environ["QL_AUTO_LOGIN"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_tasks
import notify_prefs
import push_api

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra) if extra and not cond else ""))


def wait_status(tid, want, timeout=10):
    for _ in range(int(timeout * 10)):
        t = agent_tasks.get_task(tid)
        if t and (t["status"] == want or (isinstance(want, tuple) and t["status"] in want)):
            return t
        time.sleep(0.1)
    return agent_tasks.get_task(tid)


def quick_runner(text="done-result"):
    def run(prompt, on_chunk, cancel_event):
        on_chunk("chunk1 ")
        on_chunk("chunk2 ")
        return text
    return run


def blocking_runner():
    def run(prompt, on_chunk, cancel_event):
        while not cancel_event.is_set():
            time.sleep(0.05)
        raise agent_tasks._Cancelled()
    return run


# 通知捕获
enqueued = []
gate_kinds = []
_real_enqueue = push_api.enqueue
_real_gate = notify_prefs.check_and_count
push_api.enqueue = lambda text: (enqueued.append(text), (True, ""))[1]
notify_prefs.check_and_count = lambda kind: (gate_kinds.append(kind), (True, ""))[1]

# 1. 创建成功
agent_tasks.set_runner(quick_runner())
ok, p = agent_tasks.create_task("帮我整理今天的待办")
check("1 create ok", ok and p.get("task_id"), p)
tid_done = p.get("task_id")

# 2. 空 prompt 拒绝
ok, p = agent_tasks.create_task("   ")
check("2 empty prompt rejected", not ok and "error" in p, p)

# 3. 超长 prompt 拒绝
ok, p = agent_tasks.create_task("x" * 4001)
check("3 long prompt rejected", not ok, p)

# 4. 列表包含
lst = agent_tasks.list_tasks()
check("4 list contains", any(t["id"] == tid_done for t in lst)
      and all(set(("id", "title", "status", "progress", "created_at", "updated_at")) <= set(t) for t in lst))

# 5. 详情 / 未知
t = wait_status(tid_done, "done")
check("5 done flow", t["status"] == "done" and t["progress"] == 100
      and t["result"] == "done-result" and t["summary"] == "done-result"
      and t["progress_note"] == "", t["status"])
check("5b unknown detail", agent_tasks.get_task("nope123") is None)

# 6. 失败流
def boom(prompt, on_chunk, cancel_event):
    raise RuntimeError("hermes 炸了")
agent_tasks.set_runner(boom)
ok, p = agent_tasks.create_task("会失败的任务")
t = wait_status(p["task_id"], "failed")
check("6 failed flow", t["status"] == "failed" and "hermes 炸了" in t["error"], t.get("error"))

# 7. 取消
agent_tasks.set_runner(blocking_runner())
ok, p = agent_tasks.create_task("可取消的任务")
tid_cancel = p["task_id"]
time.sleep(0.3)
check("7 running before cancel", agent_tasks.get_task(tid_cancel)["status"] == "running")
ok, r = agent_tasks.cancel_task(tid_cancel)
check("7 cancel ok", ok and r["status"] == "cancelled", r)

# 8. 取消未知 / 取消已完成
ok, r = agent_tasks.cancel_task("nope123")
check("8 cancel unknown", not ok and "error" in r, r)
ok, r = agent_tasks.cancel_task(tid_done)
check("8b cancel finished noop", ok and r["status"] == "done", r)

# 9. 完成通知入队（含 task_id）
for _ in range(50):
    if any(tid_done in e and "完成" in e for e in enqueued):
        break
    time.sleep(0.1)
check("9 done notified", any(tid_done in e and "完成" in e for e in enqueued), enqueued)

# 10. 失败通知入队（含 task_id）
for _ in range(50):
    if any("失败" in e and "task_id=" in e for e in enqueued):
        break
    time.sleep(0.1)
check("10 failed notified", any("失败" in e and "task_id=" in e for e in enqueued), enqueued)

# 11. 取消不通知
n0 = len(enqueued)
agent_tasks.set_runner(blocking_runner())
ok, p = agent_tasks.create_task("取消不通知")
time.sleep(0.2)
agent_tasks.cancel_task(p["task_id"])
time.sleep(0.3)
check("11 cancel no notify", len(enqueued) == n0, len(enqueued) - n0)

# 12. 频次闸拒绝 → 不入队
notify_prefs.check_and_count = lambda kind: (False, "上限")
n0 = len(enqueued)
agent_tasks.set_runner(quick_runner("x"))
ok, p = agent_tasks.create_task("被闸掉的任务")
wait_status(p["task_id"], "done")
check("12 gate denied no enqueue", len(enqueued) == n0)
notify_prefs.check_and_count = lambda kind: (gate_kinds.append(kind), (True, ""))[1]

# 13. 失败走 task_fail kind，完成走 task_done kind
gate_kinds.clear()
agent_tasks.set_runner(quick_runner("y"))
ok, p = agent_tasks.create_task("kind 检查")
wait_status(p["task_id"], "done")
agent_tasks.set_runner(boom)
ok, p = agent_tasks.create_task("kind 检查2")
wait_status(p["task_id"], "failed")
for _ in range(50):  # _notify 在状态落 done/failed 之后异步执行，等一下
    if "task_done" in gate_kinds and "task_fail" in gate_kinds:
        break
    time.sleep(0.1)
check("13 gate kinds", "task_done" in gate_kinds and "task_fail" in gate_kinds, gate_kinds)

# 14. 落盘：文件存在、0600、JSON 合法
fp = os.path.join(TMP, "agent_tasks.json")
st = os.stat(fp)
check("14 persisted 0600", stat.S_IMODE(st.st_mode) == 0o600, oct(stat.S_IMODE(st.st_mode)))
d = json.load(open(fp, encoding="utf-8"))
check("14b valid json", isinstance(d.get("tasks"), list))

# 15. 重启恢复：running → interrupted
d["tasks"].append({"id": "deadbeef01", "title": "僵尸", "prompt": "x",
                   "status": "running", "progress": 42, "progress_note": "约数",
                   "summary": "", "result": "", "error": "",
                   "created_at": time.time(), "updated_at": time.time()})
json.dump(d, open(fp, "w", encoding="utf-8"), ensure_ascii=False)
agent_tasks._load()
t = agent_tasks.get_task("deadbeef01")
check("15 restart interrupted", t and t["status"] == "interrupted", t and t["status"])

# 16. 只保留最近 100 条
with agent_tasks._lock:
    base = time.time()
    for i in range(105):
        agent_tasks._tasks["bulk%03d" % i] = {
            "id": "bulk%03d" % i, "title": "b", "prompt": "x", "status": "done",
            "progress": 100, "progress_note": "", "summary": "", "result": "",
            "error": "", "created_at": base + i, "updated_at": base + i}
agent_tasks._persist(force=True)
d = json.load(open(fp, encoding="utf-8"))
check("16 keep 100", len(d["tasks"]) == 100, len(d["tasks"]))
# 清理 bulk，恢复后续测试环境
with agent_tasks._lock:
    for i in range(105):
        agent_tasks._tasks.pop("bulk%03d" % i, None)

# 17. 进度启发式：执行中 progress 在 10~95 且标注约数
prog_seen = []
started = threading.Event()
def slow_chunks(prompt, on_chunk, cancel_event):
    started.wait(5)
    for i in range(30):
        on_chunk("z" * 200)
        prog_seen.append(agent_tasks.get_task(cur[0])["progress"])
        time.sleep(0.05)
    return "ok"
agent_tasks.set_runner(slow_chunks)
cur = []
ok, p = agent_tasks.create_task("进度观察")
cur.append(p["task_id"])
started.set()
t = wait_status(p["task_id"], "done", timeout=15)
check("17 progress heuristic",
      t["status"] == "done" and t["progress"] == 100
      and any(10 <= x <= 95 for x in prog_seen), (prog_seen[:5], t["progress"]))

# ---- HTTP 全链路 ----
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), agent_tasks.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# 18. HTTP 创建
agent_tasks.set_runner(quick_runner("http-ok"))
s, r = http("POST", "/api/agent/tasks", {"prompt": "http 任务"})
check("18 http create", s == 200 and r.get("ok") and r.get("task_id"), (s, r))
htid = r.get("task_id")

# 19. HTTP 列表
s, r = http("GET", "/api/agent/tasks")
check("19 http list", s == 200 and any(t["id"] == htid for t in r.get("tasks", [])), s)

# 20. HTTP 详情（含 result，不含 prompt）
t = wait_status(htid, "done")
s, r = http("GET", "/api/agent/tasks/" + htid)
check("20 http detail", s == 200 and r["task"]["result"] == "http-ok"
      and "prompt" not in r["task"], (s, r.get("task", {}).get("result")))

# 21. HTTP 取消执行中
agent_tasks.set_runner(blocking_runner())
s, r = http("POST", "/api/agent/tasks", {"prompt": "http 取消"})
ctid = r["task_id"]
time.sleep(0.3)
s, r = http("POST", "/api/agent/tasks/%s/cancel" % ctid)
check("21 http cancel", s == 200 and r["status"] == "cancelled", (s, r))

# 22. HTTP 未知任务 404
s, r = http("GET", "/api/agent/tasks/nope999")
check("22 http 404", s == 404 and not r.get("ok"), s)

# 23. HTTP 空 prompt → ok=false
s, r = http("POST", "/api/agent/tasks", {"prompt": ""})
check("23 http empty", s == 200 and not r.get("ok"), r)

push_api.enqueue = _real_enqueue
notify_prefs.check_and_count = _real_gate

print("\n%d/%d 通过" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("FAIL:", FAIL)
    sys.exit(1)
