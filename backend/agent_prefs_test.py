#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_prefs 自测：资讯点赞 / 动作权限 / 产物沉淀 / 媒体生成 / 简报口径。

跑法：cd backend && python3 agent_prefs_test.py
只依赖标准库；不碰真实数据目录（STREAM_DATA_DIR 指向临时目录）；
走真实 hermes_api.Handler 的 HTTP 全链路（含鉴权）。
"""
import json
import os
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_tmp = tempfile.mkdtemp(prefix="prefs_test_")
os.environ["STREAM_DATA_DIR"] = _tmp
os.environ["QL_AUTO_LOGIN"] = "1"

import hermes_api  # noqa: E402
import agent_prefs  # noqa: E402

api_srv = ThreadingHTTPServer(("127.0.0.1", 0), hermes_api.Handler)
api_port = api_srv.server_address[1]
threading.Thread(target=api_srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % api_port


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name +
          (" — " + str(extra) if extra and not cond else ""))


def J(b):
    return json.loads(b)

# ---------------------------------------------------------------- 点赞
s, b = http("GET", "/api/agent/brief/likes")
check("likes 初始为空", s == 200 and J(b)["liked_ids"] == [])

s, b = http("POST", "/api/agent/brief/like", {"article_id": "a1", "liked": True})
check("点赞 ok", s == 200 and J(b)["ok"] is True)
s, b = http("POST", "/api/agent/brief/like", {"article_id": "a2", "liked": True})
s, b = http("GET", "/api/agent/brief/likes")
check("likes 含 a1/a2", set(J(b)["liked_ids"]) == {"a1", "a2"})

s, b = http("POST", "/api/agent/brief/like", {"article_id": "a1", "liked": False})
check("取消点赞 ok", s == 200 and J(b)["ok"] is True)
s, b = http("GET", "/api/agent/brief/likes")
check("取消后只剩 a2", J(b)["liked_ids"] == ["a2"])

s, b = http("POST", "/api/agent/brief/like", {"liked": True})
check("缺 article_id 拒绝", s == 200 and J(b)["ok"] is False)
s, b = http("POST", "/api/agent/brief/like", {"article_id": "a3", "liked": "yes"})
check("liked 非布尔拒绝", s == 200 and J(b)["ok"] is False)

# ---------------------------------------------------------------- 动作权限
s, b = http("GET", "/api/agent/action-policy")
p = J(b)["policy"]
check("policy 默认 7 键全 ask",
      s == 200 and set(p) == set(agent_prefs.ACTION_KEYS)
      and all(v == "ask" for v in p.values()), p)

s, b = http("POST", "/api/agent/action-policy",
            {"policy": {"send_message": "deny", "web_search": "allow"}})
d = J(b)
check("policy 部分更新 ok", s == 200 and d["ok"] is True
      and d["policy"]["send_message"] == "deny"
      and d["policy"]["web_search"] == "allow"
      and d["policy"]["read_calendar"] == "ask")

s, b = http("POST", "/api/agent/action-policy",
            {"policy": {"send_message": "maybe"}})
check("非法值拒绝", s == 200 and J(b)["ok"] is False)
s, b = http("POST", "/api/agent/action-policy",
            {"policy": {"delete_everything": "deny"}})
check("未知动作拒绝", s == 200 and J(b)["ok"] is False)
s, b = http("GET", "/api/agent/action-policy")
check("非法写入未污染", J(b)["policy"]["send_message"] == "deny")

# ---------------------------------------------------------------- 产物沉淀
s, b = http("GET", "/api/agent/artifacts")
check("artifacts 初始为空", s == 200 and J(b)["artifacts"] == [])

s, b = http("POST", "/api/agent/artifacts",
            {"title": "第一篇", "kind": "markdown", "content": "# hi"})
art = J(b)["artifact"]
check("创建 artifact ok", s == 200 and J(b)["ok"] is True
      and art["title"] == "第一篇" and art["kind"] == "markdown"
      and len(art["id"]) == 32 and art["created_at"].endswith("Z"))
aid = art["id"]

s, b = http("GET", "/api/agent/artifacts")
check("列表含新建", len(J(b)["artifacts"]) == 1
      and J(b)["artifacts"][0]["id"] == aid)

s, b = http("POST", "/api/agent/artifacts", {"title": "", "content": "x"})
check("缺 title 拒绝", s == 200 and J(b)["ok"] is False)
s, b = http("POST", "/api/agent/artifacts", {"title": "t"})
check("缺 content 拒绝", s == 200 and J(b)["ok"] is False)

s, b = http("DELETE", "/api/agent/artifacts", {"id": "nope"})
check("删不存在 → not_found", s == 200 and J(b)["ok"] is False)
s, b = http("DELETE", "/api/agent/artifacts", {"id": aid})
check("删除 ok", s == 200 and J(b)["ok"] is True)
s, b = http("GET", "/api/agent/artifacts")
check("删后为空", J(b)["artifacts"] == [])

# 200 上限：建 201 条，最旧被淘汰
first_id = None
for i in range(201):
    s, b = http("POST", "/api/agent/artifacts",
                {"title": "t%d" % i, "content": "c%d" % i})
    if i == 0:
        first_id = J(b)["artifact"]["id"]
s, b = http("GET", "/api/agent/artifacts")
arts = J(b)["artifacts"]
check("上限 200 条", len(arts) == 200, len(arts))
check("最旧被淘汰", all(a["id"] != first_id for a in arts))
check("新→旧排序", arts[0]["title"] == "t200")

# ---------------------------------------------------------------- 媒体生成
s, b = http("POST", "/api/agent/media/generate", {"prompt": "画只猫"})
d = J(b)
check("无凭证 → 501 media_not_configured",
      s == 501 and d["error"] == "media_not_configured" and d.get("hint"))

os.environ["QL_MEDIA_API_KEY"] = "test-key"
s, b = http("POST", "/api/agent/media/generate", {"prompt": "画只猫"})
d = J(b)
check("有凭证 → 501 media_not_implemented（不伪造）",
      s == 501 and d["error"] == "media_not_implemented")
del os.environ["QL_MEDIA_API_KEY"]

s, b = http("POST", "/api/agent/media/generate", {})
check("缺 prompt 拒绝", s == 200 and J(b)["ok"] is False)

# ---------------------------------------------------------------- 简报口径
s, b = http("GET", "/api/agent/brief")
check("brief GET 初始", s == 200 and J(b)["brief"] == ""
      and J(b)["status"] == "not_configured")

s, b = http("POST", "/api/agent/brief", {"brief": "只看AI圈"})
d = J(b)
check("brief POST v1 占位", s == 200 and d["status"] == "not_configured"
      and d["articles"] == [])
s, b = http("GET", "/api/agent/brief")
check("brief 口径已持久化", J(b)["brief"] == "只看AI圈")

# ---------------------------------------------------------------- 落盘与鉴权
mode = stat.S_IMODE(os.stat(os.path.join(_tmp, "brief_likes.json")).st_mode)
check("落盘文件 0600", mode == 0o600, oct(mode))

import auth_api  # noqa: E402
auth_api.AUTO_LOGIN = False  # 运行时关闭免登录，验证 401 链路
s, b = http("GET", "/api/agent/brief/likes")
check("无鉴权 → 401", s == 401, s)
s, b = http("POST", "/api/agent/artifacts", {"title": "t", "content": "c"})
check("无鉴权 POST → 401", s == 401, s)
auth_api.AUTO_LOGIN = True

# ----------------------------------------------------------------
n = len(results)
print("\n%d/%d 通过" % (sum(results), n))
sys.exit(0 if all(results) else 1)
