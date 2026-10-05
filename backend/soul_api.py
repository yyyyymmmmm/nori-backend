#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Soul（AI 人设/输出风格）配置 API：让 App 设置页可查看/编辑 soul。

端点（unified_router 挂载 /api/soul 前缀；另有 /api/agent/soul 别名，
借 /api/agent 前缀 → lucky 白名单/relay/nginx 零改动，蜂窝下可用）：
  GET  /api/soul   {"soul": "<当前文本>", "is_default": true/false}
                   未自定义时返回内置默认 SOUL_PROMPT，is_default=true
  POST /api/soul   {"soul": "..."} → 落盘；空字符串 = 恢复默认
                   {"ok": true} / {"ok": false, "error": "<中文原因>"}

鉴权：与其他设置类 API 一致（auth_api.check_auth + X-Hermes-Password 头）。
只依赖标准库。
"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler

import soul_store


def _default_soul():
    # 内置默认与 stream_api.SOUL_PROMPT 同源；stream_api 侧 _soul_prompt()
    # 传的就是它，这里做兜底避免循环导入。
    try:
        import stream_api
        return stream_api.SOUL_PROMPT
    except Exception:  # noqa: BLE001
        return ""


class SoulHandler(BaseHTTPRequestHandler):
    def _check_auth(self):
        import auth_api
        return auth_api.check_auth(self.headers, "X-Hermes-Password",
                                   os.environ.get("QL_PASSWORD", ""))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, X-Hermes-Password")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _is_soul(self, path):
        return path.endswith("/soul")

    def do_GET(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        path = urllib.parse.urlparse(self.path).path
        if self._is_soul(path):
            custom = soul_store.get_custom()
            if custom is not None:
                self._send(200, {"soul": custom, "is_default": False})
            else:
                self._send(200, {"soul": _default_soul(), "is_default": True})
            return
        self._send(404, {"error": "Not Found"})

    def do_POST(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:  # noqa: BLE001
            body = {}
        if self._is_soul(path):
            text = body.get("soul", "")
            text = text if isinstance(text, str) else ""
            ok, err = soul_store.save_custom(text)
            if not ok:
                self._send(200, {"ok": False, "error": err})
                return
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "Not Found"})

    def log_message(self, fmt, *args):
        pass
