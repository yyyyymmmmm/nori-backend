#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hermes_oauth 自测：本地 mock 厂商走完「厂商清单 → start → 回调换 token →
已连接 → 自动续期 → 断开」全链路，并覆盖未配凭证/坏 state/未知厂商。

跑法：cd backend && python3 hermes_oauth_test.py
只依赖标准库；不碰真实数据目录（STREAM_DATA_DIR 指向临时目录）。
"""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_tmp = tempfile.mkdtemp(prefix="oauth_test_")
os.environ["STREAM_DATA_DIR"] = _tmp
os.environ["QL_AUTO_LOGIN"] = "1"
os.environ["QL_OAUTH_MOCK_CLIENT_ID"] = "mock_id_123"
os.environ["QL_OAUTH_MOCK_CLIENT_SECRET"] = "mock_secret_456"

import hermes_oauth  # noqa: E402

# ---------------------------------------------------------------------------
# mock 厂商 token 服务
# ---------------------------------------------------------------------------
calls = {"exchange": 0, "refresh": 0}


class MockVendorHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("mock_id") != "mock_id_123" or \
                body.get("mock_secret") != "mock_secret_456":
            self._send({"error": "invalid_client"})
            return
        gt = body.get("grant_type")
        if gt == "authorization_code":
            calls["exchange"] += 1
            if body.get("code") != "mockcode123":
                self._send({"error": "invalid_grant"})
                return
            self._send({"access_token": "AT1", "refresh_token": "RT1",
                        "expires_in": 3600, "token_type": "Bearer"})
        elif gt == "refresh_token":
            calls["refresh"] += 1
            if body.get("refresh_token") != "RT1":
                self._send({"error": "invalid_grant"})
                return
            # 故意不返回新的 refresh_token，测框架的 fallback 保留旧 RT
            self._send({"access_token": "AT2", "expires_in": 3600})
        else:
            self._send({"error": "unsupported_grant_type"})

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


mock_srv = ThreadingHTTPServer(("127.0.0.1", 0), MockVendorHandler)
mock_port = mock_srv.server_address[1]
threading.Thread(target=mock_srv.serve_forever, daemon=True).start()

# 注入 mock 厂商（测完摘掉）
hermes_oauth.VENDORS["mock"] = {
    "name": "Mock 厂商",
    "capabilities": "自测",
    "icon": "🧪",
    "authorize_url": "http://127.0.0.1:%d/authorize" % mock_port,
    "authorize_params": {"response_type": "code", "scope": "test"},
    "token_url": "http://127.0.0.1:%d/token" % mock_port,
    "token_method": "POST",
    "token_body": "json",
    "id_param": "mock_id",
    "secret_param": "mock_secret",
    "token_extra": {"grant_type": "authorization_code"},
    "refresh_grant": "refresh_token",
}

# ---------------------------------------------------------------------------
# 被测后端（真实 hermes_api.Handler）
# ---------------------------------------------------------------------------
import hermes_api  # noqa: E402

api_srv = ThreadingHTTPServer(("127.0.0.1", 0), hermes_api.Handler)
api_port = api_srv.server_address[1]
threading.Thread(target=api_srv.serve_forever, daemon=True).start()
os.environ["QL_OAUTH_REDIRECT_BASE"] = "http://127.0.0.1:%d" % api_port
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
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (" — " + str(extra) if extra and not cond else ""))


# 1. 厂商清单：5 真实 + mock，初始都未连接
s, b = http("GET", "/api/hermes/oauth/vendors")
d = json.loads(b)
ids = [v["id"] for v in d["vendors"]]
check("vendors 含 5 真实厂商", all(i in ids for i in
      ["feishu", "dingtalk", "wecom", "tencent_docs", "baidu_netdisk"]), ids)
check("初始 connected 全 false", all(v["connected"] is False for v in d["vendors"]))
check("vendor 字段齐全", all(all(k in v for k in
      ("id", "name", "capabilities", "connected", "icon")) for v in d["vendors"]))
check("token 永不外泄", "AT1" not in b.decode() and "mock_secret" not in b.decode())

# 2. 未配凭证 → oauth_not_configured + 中文 hint
s, b = http("POST", "/api/hermes/oauth/start", {"vendor_id": "feishu"})
d = json.loads(b)
check("未配凭证报 oauth_not_configured",
      d.get("ok") is False and d.get("error") == "oauth_not_configured", d)
check("hint 含自托管指引", "QL_OAUTH_FEISHU_CLIENT_ID" in d.get("hint", ""), d.get("hint", "")[:60])

# 3. 未知厂商
s, b = http("POST", "/api/hermes/oauth/start", {"vendor_id": "nosuch"})
check("未知厂商报 unknown_vendor", json.loads(b).get("error") == "unknown_vendor")

# 4. mock start → auth_url 含三要素
s, b = http("POST", "/api/hermes/oauth/start", {"vendor_id": "mock"})
d = json.loads(b)
q = urllib.parse.parse_qs(urllib.parse.urlparse(d.get("auth_url", "")).query)
check("start 返回 auth_url", d.get("ok") is True and d.get("auth_url", "").startswith("http"))
check("auth_url 含 client_id/state/redirect_uri",
      q.get("mock_id") == ["mock_id_123"] and "state" in q and "redirect_uri" in q)
state = q["state"][0]

# 5. 回调换 token → 成功页
s, b = http("GET", "/api/hermes/oauth/callback?code=mockcode123&state=" + state)
html = b.decode()
check("回调返回成功 HTML", s == 200 and "连接成功" in html and "请返回 App" in html)
check("换 token 调了一次厂商", calls["exchange"] == 1, calls)

# 6. 已连接 + 落盘 0600
s, b = http("GET", "/api/hermes/oauth/vendors")
mock = [v for v in json.loads(b)["vendors"] if v["id"] == "mock"][0]
check("回调后 connected=true", mock["connected"] is True)
tp = os.path.join(_tmp, "oauth_tokens.json")
mode = stat.S_IMODE(os.stat(tp).st_mode)
check("token 文件 0600", mode == 0o600, oct(mode))
stored = json.load(open(tp, encoding="utf-8"))["mock"]
check("落盘含 refresh_token", stored.get("refresh_token") == "RT1")

# 7. state 一次性：重放同一 state 应失败
s, b = http("GET", "/api/hermes/oauth/callback?code=mockcode123&state=" + state)
check("state 重放被拒绝", "state 无效或已过期" in b.decode())

# 8. 坏 state
s, b = http("GET", "/api/hermes/oauth/callback?code=x&state=bogus")
check("坏 state 被拒绝", "state 无效或已过期" in b.decode())

# 9. 自动续期：把 expires_at 写成过去 → 下次读取触发 refresh
stored["expires_at"] = int(time.time()) - 10
d = json.load(open(tp, encoding="utf-8"))
d["mock"] = stored
json.dump(d, open(tp, "w", encoding="utf-8"), ensure_ascii=False)
tok = hermes_oauth.get_access_token("mock")
check("过期触发自动续期", tok == "AT2" and calls["refresh"] == 1, (tok, calls))
new_stored = json.load(open(tp, encoding="utf-8"))["mock"]
check("续期后 RT 被保留（fallback）", new_stored.get("refresh_token") == "RT1")
check("续期后仍 connected", hermes_oauth.is_connected("mock") is True)

# 10. 断开
s, b = http("POST", "/api/hermes/oauth/disconnect", {"vendor_id": "mock"})
check("disconnect ok", json.loads(b).get("ok") is True)
check("断开后 connected=false", hermes_oauth.is_connected("mock") is False)

# 11. 别名前缀可用
s, b = http("GET", "/api/agent/oauth/vendors")
check("/api/agent/oauth 别名 200", s == 200 and "vendors" in json.loads(b))

# 12. disconnect 未知厂商
s, b = http("POST", "/api/hermes/oauth/disconnect", {"vendor_id": "nosuch"})
check("disconnect 未知厂商 ok=false", json.loads(b).get("ok") is False)

mock_srv.shutdown()
api_srv.shutdown()
del hermes_oauth.VENDORS["mock"]

fails = [n for n, ok, _ in results if not ok]
print("\n%d/%d 通过" % (len(results) - len(fails), len(results)))
sys.exit(1 if fails else 0)
