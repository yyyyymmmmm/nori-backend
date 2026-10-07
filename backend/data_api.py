#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据导出：GET /api/data/export?format=json|zip。

打包用户数据：Hermes 原生记忆 + 定时任务配置 + 通知偏好。
**不含任何 token/密钥**：只取白名单字段，导出前做键名扫描兜底
（含 key/token/secret/password/auth/credential 的键直接丢弃）。

端点（unified_router 9127 挂载 /api/data 前缀；另有 /api/agent/data 别名，
借 /api/agent 前缀 → lucky 白名单/relay/nginx 三处零改动）：
  GET /api/data/export?format=json  → {"exported_at", "memory",
                                       "cron_tasks", "notify_prefs"}（下载 JSON）
  GET /api/data/export?format=zip   → zip 下载（标准库 zipfile）：
                                       hermes_memory.json /
                                       cron_tasks.json / notify_prefs.json /
                                       README.txt

鉴权：与其他设置类 API 一致（auth_api.check_auth + X-Data-Password 头）。
只依赖标准库。
"""
import io
import json
import os
import time
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler

import hermes_upstream
import notify_prefs

# 键名黑名单（大小写不敏感）：命中则丢弃，纵深防御
_SECRET_KEY_HINTS = ("key", "token", "secret", "password", "passwd",
                     "auth", "credential", "private")


def _scrub(obj):
    """递归丢弃键名命中黑名单的字段；返回清洗后的副本。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(h in kl for h in _SECRET_KEY_HINTS):
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def _fetch_cron_tasks():
    """从 Hermes 拉任务配置（白名单字段）；失败返回 []。"""
    try:
        req = urllib.request.Request(
            "%s/api/jobs?include_disabled=true" % hermes_upstream.get_base_url(),
            headers={"Authorization": "Bearer %s" % hermes_upstream.get_key()})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        jobs = data.get("jobs", [])
        if not isinstance(jobs, list):
            return []
        out = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            out.append({
                "id": job.get("id", ""),
                "name": job.get("name", "未命名"),
                "cron": job.get("schedule_display",
                                job.get("schedule", {}).get("display", "")),
                "prompt": job.get("prompt", ""),
                "enabled": job.get("enabled", True),
                "deliver": job.get("deliver", "origin"),
            })
        return out
    except Exception:  # noqa: BLE001
        return []


def build_export():
    """组装导出字典（已清洗，不含密钥）。"""
    memory = {}
    try:
        import hermes_inspect
        memory = hermes_inspect.get_hermes_memory()
    except Exception:
        pass
    payload = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": "qingliao",
        "memory": memory,
        "cron_tasks": _fetch_cron_tasks(),
        "notify_prefs": notify_prefs.get_prefs(),
    }
    return _scrub(payload)


_EXPORT_README = """轻聊数据导出
================
导出时间：{exported_at}

文件说明：
  hermes_memory.json Hermes 原生 MEMORY.md / USER.md 内容
  cron_tasks.json   定时任务配置
  notify_prefs.json 通知偏好

安全说明：导出不含任何 token / 密钥 / 密码。
如需迁移到新设备，在新后端上通过对应 API 重新导入即可。
"""


def build_zip(payload):
    """把导出字典打成 zip bytes。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("hermes_memory.json",
                    json.dumps({"files": payload["memory"]},
                               ensure_ascii=False, indent=2))
        zf.writestr("cron_tasks.json",
                    json.dumps({"tasks": payload["cron_tasks"]},
                               ensure_ascii=False, indent=2))
        zf.writestr("notify_prefs.json",
                    json.dumps(payload["notify_prefs"],
                               ensure_ascii=False, indent=2))
        zf.writestr("README.txt", _EXPORT_README.format(
            exported_at=payload["exported_at"]))
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def _check_auth(self):
        import auth_api
        return auth_api.check_auth(self.headers, "X-Data-Password",
                                   os.environ.get("QL_PASSWORD", ""))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Auth-Token, X-Data-Password")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def _send(self, code, obj, filename=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        if filename:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % filename)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_zip(self, data, filename):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % filename)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _is_export(self, path):
        return path.endswith("/data/export")

    def do_GET(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        parsed = urllib.parse.urlparse(self.path)
        if self._is_export(parsed.path):
            qs = urllib.parse.parse_qs(parsed.query)
            fmt = (qs.get("format") or ["json"])[0].lower()
            payload = build_export()
            stamp = time.strftime("%Y%m%d%H%M%S")
            if fmt == "zip":
                self._send_zip(build_zip(payload),
                               "qingliao-export-%s.zip" % stamp)
            else:
                self._send(200, payload,
                           filename="qingliao-export-%s.json" % stamp)
            return
        self._send(404, {"error": "Not Found"})

    def log_message(self, *a):
        pass
