#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes 上游配置 API：让 App 设置页可配 Hermes 连接地址/密钥、切换模型、同步模型列表。

端点（unified_router 9127 挂载 /api/hermes 前缀；另有 /api/agent/hermes 别名，
借 /api/agent 前缀 → lucky 白名单/relay/nginx 三处零改动）：
  GET  /api/hermes/upstream   当前上游地址 + 是否已配 key（key 本身永不返回）
  POST /api/hermes/upstream   {url, key} → 先实测连通再落盘；失败不保存
  GET  /api/hermes/models     代理 Hermes /v1/models（无结果回退 /api/model/options），60 秒缓存；
                             每项带 selected 标记当前选中模型
  POST /api/hermes/model      {model_id} → 校验（须在 Hermes 实时模型列表中）后落盘选中

鉴权：与其他设置类 API 一致（auth_api.check_auth + X-Hermes-Password 头）。
只依赖标准库。
"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler

import hermes_upstream


class Handler(BaseHTTPRequestHandler):
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

    def _is_upstream(self, path):
        return path.endswith("/hermes/upstream")

    def _is_models(self, path):
        return path.endswith("/hermes/models")

    def _is_model(self, path):
        return path.endswith("/hermes/model")

    def do_GET(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        path = urllib.parse.urlparse(self.path).path
        if self._is_upstream(path):
            key = hermes_upstream.get_key()
            self._send(200, {
                "url": hermes_upstream.get_base_url(),
                "has_key": bool(key),
            })
            return
        if self._is_models(path):
            models, err = hermes_upstream.get_models()
            if err:
                self._send(200, {"models": [], "error": err})
            else:
                sel = hermes_upstream.get_selected_model()
                for m in models:
                    m["selected"] = (m["id"] == sel)
                self._send(200, {"models": models})
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
        if self._is_upstream(path):
            url = str(body.get("url", "") or "")
            key = str(body.get("key", "") or "")
            ok, err = hermes_upstream.test_upstream(url, key)
            if not ok:
                self._send(200, {"ok": False, "error": err})
                return
            ok2, err2 = hermes_upstream.save_upstream(url, key)
            if not ok2:
                self._send(200, {"ok": False, "error": err2})
                return
            self._send(200, {"ok": True})
            return
        if self._is_model(path):
            mid = str(body.get("model_id", "") or "")
            ok, err = hermes_upstream.save_selected_model(mid)
            if not ok:
                self._send(200, {"ok": False, "error": err})
                return
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "Not Found"})
