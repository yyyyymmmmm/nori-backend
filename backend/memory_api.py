#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 记忆 API：GET /api/memory/list、POST /api/memory/add|delete|update|import"""
import json
import os
from http.server import BaseHTTPRequestHandler

import memory_store

# 记忆导入支持的来源（POST /api/memory/import 的 source 字段）
IMPORT_SOURCES = ("chatgpt", "claude", "gemini")
_IMPORT_ITEM_CAP = 500  # 单次最多处理条数，防刷


def _extract_memory_text(item):
    """宽容解析一条导入条目 → 纯文本；解析不出返回 ""。

    支持形状：
      - "纯字符串"
      - {"content": "..."} / {"text": "..."} / {"message": "..."}
      - {"content": {"parts": [...]}}（ChatGPT conversations 形状）
      - {"content": ["a", "b"]}（列表拼起来）
    """
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in ("content", "text", "message"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            parts = v.get("parts")
            if isinstance(parts, list):
                t = " ".join(str(p).strip() for p in parts
                             if isinstance(p, str) and p.strip())
                if t:
                    return t
        if isinstance(v, list):
            t = " ".join(str(p).strip() for p in v
                         if isinstance(p, str) and p.strip())
            if t:
                return t
    return ""


class MemoryHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        import auth_api
        return auth_api.check_auth(self.headers, "X-Memory-Password",
                                   os.environ.get("QL_PASSWORD", ""))

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/memory/list"):
            entries = memory_store.list_entries()
            # 2026-10-07：同时返回 Hermes 真实记忆（唯一真源）
            # iOS 记忆页会合并展示
            try:
                import hermes_inspect
                hermes_mem = hermes_inspect.get_hermes_memory()
                hermes_text = ""
                if hermes_mem.get("MEMORY.md"):
                    hermes_text += hermes_mem["MEMORY.md"][:2000]
                if hermes_mem.get("USER.md"):
                    hermes_text += "\n\n--- 关于用户 ---\n" + hermes_mem["USER.md"][:1000]
                if hermes_text.strip():
                    # 作为特殊条目插入最前面
                    entries = [{
                        "text": hermes_text.strip(),
                        "status": "active",
                        "source": "hermes",
                        "sessionId": "",
                        "created": None,
                        "updated": None,
                    }] + entries
            except Exception:
                pass
            self._send(200, {"ok": True, "entries": entries})
            return
        self._send(404, {"error": "Not Found"})

    def do_POST(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            body = {}
        if parsed.path.startswith("/api/memory/add"):
            text = (body.get("text") or "").strip()
            if not text:
                self._send(200, {"ok": False, "message": "内容不能为空"})
                return
            memory_store.add_entry(text)
            self._send(200, {"ok": True, "message": "已记住", "entries": memory_store.list_entries()})
            return
        if parsed.path.startswith("/api/memory/delete"):
            text = (body.get("text") or "").strip()
            memory_store.delete_entry(text)
            self._send(200, {"ok": True, "message": "已删除", "entries": memory_store.list_entries()})
            return
        if parsed.path.startswith("/api/memory/update"):
            # v3.9.40（#19）：App 记忆面板就地编辑
            old = (body.get("old") or "").strip()
            new = (body.get("text") or "").strip()
            if not new or len(new) < 2:
                self._send(200, {"ok": False, "message": "内容不能为空",
                                 "entries": memory_store.list_entries()})
                return
            ok = memory_store.update_entry(old, new)
            self._send(200, {"ok": ok,
                             "message": "已更新" if ok else "更新失败（原条目不存在或写入出错）",
                             "entries": memory_store.list_entries()})
            return
        if parsed.path.startswith("/api/memory/import"):
            # 从 ChatGPT / Claude / Gemini 导入记忆：宽容解析 items，
            # 逐条经 add_entry 口径写入（去重、上限 50 走既有逻辑）。
            # body 形状：{"source": "chatgpt|claude|gemini", "items": [...]}
            # 或顶层直接是 [...]。
            source = str(body.get("source", "") or "").lower() \
                if isinstance(body, dict) else ""
            items = body.get("items") if isinstance(body, dict) else None
            if items is None and isinstance(body, list):
                items = body
            if source and source not in IMPORT_SOURCES:
                self._send(200, {"ok": False,
                                 "message": "未知来源（支持 chatgpt/claude/gemini）"})
                return
            if not isinstance(items, list):
                self._send(200, {"ok": False, "message": "items 须为数组"})
                return
            imported, skipped = 0, 0
            for it in items[:_IMPORT_ITEM_CAP]:
                text = _extract_memory_text(it)
                if not text or len(text) < 2:
                    skipped += 1
                    continue
                if memory_store.add_entry(text):
                    imported += 1
                else:
                    skipped += 1  # 重复或写入失败
            self._send(200, {"ok": True, "imported": imported,
                             "skipped": skipped,
                             "entries": memory_store.list_entries()})
            return
        self._send(404, {"error": "Not Found"})

    def log_message(self, fmt, *args):
        pass
