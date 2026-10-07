#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 记忆 API：GET /api/memory/list、POST /api/memory/add|delete|update|import"""
import json
import os
from http.server import BaseHTTPRequestHandler

# 记忆导入支持的来源（POST /api/memory/import 的 source 字段）
IMPORT_SOURCES = ("chatgpt", "claude", "gemini")
_IMPORT_ITEM_CAP = 500  # 单次最多处理条数，防刷


def _memory_list_payload():
    # Hermes owns persistent AI memory. Do not merge Nori's legacy memory.json
    # into the active list; leaving it on disk preserves an export/rollback path.
    entries = []
    items = []
    hermes_status = {"connected": False, "count": 0}
    try:
        import hermes_inspect
        container = hermes_inspect.find_hermes_container()
        if not container:
            raise RuntimeError("Hermes 容器未找到")
        hermes_mem = hermes_inspect.get_hermes_memory()
        hermes_text = ""
        if hermes_mem.get("MEMORY.md"):
            hermes_text += hermes_mem["MEMORY.md"][:2000]
        if hermes_mem.get("USER.md"):
            hermes_text += "\n\n--- 关于用户 ---\n" + hermes_mem["USER.md"][:1000]
        hermes_text = hermes_text.strip()
        hermes_status = {"connected": True, "count": sum(bool(hermes_mem.get(k)) for k in ("MEMORY.md", "USER.md"))}
        if hermes_text:
            entries.append(hermes_text)
            items.append({"text": hermes_text, "status": "active",
                          "source": "hermes", "sessionId": "",
                          "created": None, "updated": None})
    except Exception as exc:  # noqa: BLE001
        hermes_status = {"connected": False, "count": 0,
                         "error": str(exc)[:160]}
    return {"entries": entries, "items": items,
            "hermes_status": hermes_status}


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
            # Keep the legacy `entries` contract strictly string-only. The iOS
            # MemoryEntry parser intentionally rejects a mixed [object, string]
            # array, which previously made both Hermes and local memories vanish.
            payload = _memory_list_payload()
            self._send(200, {"ok": True, **payload})
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
            # Manual memories from this screen belong to Hermes's live memory.
            # Do not also mirror them locally: that creates duplicate entries and
            # makes deletion appear ineffective while the Hermes copy survives.
            try:
                import hermes_inspect
                ok, detail = hermes_inspect.append_hermes_memory(text)
            except Exception as exc:
                ok, detail = False, str(exc)[:160]
            if not ok:
                self._send(503, {"ok": False, "message": "写入 Hermes 记忆失败",
                                 "error": str(detail)[:160]})
                return
            payload = _memory_list_payload()
            self._send(200, {"ok": True, "message": "已写入 Hermes 记忆",
                             **payload})
            return
        if parsed.path.startswith("/api/memory/delete"):
            self._send(410, {"ok": False,
                             "message": "Nori 本地记忆已停用。请在 Hermes 对话中要求它修改或忘记记忆。"})
            return
        if parsed.path.startswith("/api/memory/update"):
            self._send(410, {"ok": False,
                             "message": "Nori 本地记忆已停用。请在 Hermes 对话中要求它修改或忘记记忆。"})
            return
        if parsed.path.startswith("/api/memory/status"):
            self._send(410, {"ok": False,
                             "message": "记忆状态由 Hermes 管理；Nori 不保存本地副本。"})
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
            import hermes_inspect
            for it in items[:_IMPORT_ITEM_CAP]:
                text = _extract_memory_text(it)
                if not text or len(text) < 2:
                    skipped += 1
                    continue
                ok, _ = hermes_inspect.append_hermes_memory(text)
                if ok:
                    imported += 1
                else:
                    skipped += 1
            self._send(200, {"ok": True, "imported": imported,
                             "skipped": skipped,
                             **_memory_list_payload()})
            return
        self._send(404, {"error": "Not Found"})

    def log_message(self, fmt, *args):
        pass
