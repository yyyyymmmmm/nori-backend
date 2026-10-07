#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes 上游配置 API：让 App 设置页可配 Hermes 连接地址/密钥、切换模型、同步模型列表。

端点（unified_router 9127 挂载 /api/hermes 前缀；另有 /api/agent/hermes 别名，
借 /api/agent 前缀 → lucky 白名单/relay/nginx 三处零改动）：
  GET  /api/hermes/upstream   当前上游地址 + 是否已配 key（key 本身永不返回）
  POST /api/hermes/upstream   {url, key} → 先实测连通再落盘；失败不保存
  GET  /api/hermes/models     按服务商分组：{providers: [{id, name, models: [{id, name,
                             selected}], error}]}；单家挂了只标 error；60 秒缓存；已过滤隐藏
  POST /api/hermes/model      {provider, model_id} → 校验（须在该服务商实时列表中）后落盘
                              （兼容老 {model_id}，provider 缺省=当前选中）
  GET  /api/hermes/providers  服务商清单：id/name/has_key/is_default/editable（key 永不返回）
  POST /api/hermes/providers  {name, url, key} → 先实测连通再落盘
  DELETE /api/hermes/providers/{id} → 删除用户服务商（default 不许删）
  POST /api/hermes/models/hide {provider, model_ids: [...]} → 整体替换该服务商隐藏名单
  GET  /api/hermes/platforms  第三方平台清单：id/name/configured/enabled/needs（token 永不返回）
  POST /api/hermes/platforms  {platform, enabled, config} → 校验必填项后写 config.yaml
                             platforms 段并重启 gateway（扫码/配对类平台第一版只读状态）
  GET  /api/hermes/skills     技能清单：id/name/description/enabled（enabled=不在
                             config.yaml skills.disabled 列表；与 `hermes skills config`
                             官方 CLI 读写同一位置）
  POST /api/hermes/skills     {skill_id, enabled} → 改 skills.disabled 并重启 gateway
                             （改完必须重启才生效，官方 FAQ 原话）
  GET  /api/hermes/oauth/vendors   云服务连接器厂商清单：id/name/capabilities/connected/icon
  POST /api/hermes/oauth/start      {vendor_id} → {"auth_url"}（点按授权第一步）
  GET  /api/hermes/oauth/callback  浏览器回调（免鉴权，靠 state 防 CSRF）：code 换 token
                                   落盘，返回「已完成，请返回 App」HTML
  POST /api/hermes/oauth/disconnect {vendor_id} → {"ok": true}
  （以上 oauth 四条另有 /api/agent/oauth/* 别名，借 /api/agent 前缀）
  POST /api/agent/brief/like   {article_id, liked} → {"ok": true}
  GET  /api/agent/brief/likes  → {"liked_ids": [...]}
  GET  /api/agent/action-policy → {"policy": {read_calendar: ask, ...}}（7 类动作）
  POST /api/agent/action-policy {policy: {...}} → {"ok": true, "policy": {...}}
  GET  /api/agent/artifacts    → {"artifacts": [...]}（新→旧，上限 200）
  POST /api/agent/artifacts    {title, kind, content} → {"ok": true, "artifact"}
  DELETE /api/agent/artifacts  {id} → {"ok": true}
  POST /api/agent/media/generate {prompt} → 501（如实未配置/未实现，不伪造图片）
  GET  /api/agent/brief        → {"brief", "status"}
  POST /api/agent/brief        {brief} → {"status": "not_configured", "articles": []}
  （以上十条见 agent_prefs.py；借 /api/agent 前缀 → lucky 白名单零改动）

鉴权：与其他设置类 API 一致（auth_api.check_auth + X-Hermes-Password 头）。
只依赖标准库。
"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler

import hermes_upstream
import hermes_platforms
import hermes_skills
import hermes_oauth
import agent_prefs


class Handler(BaseHTTPRequestHandler):
    def _check_auth(self):
        import auth_api
        return auth_api.check_auth(self.headers, "X-Hermes-Password",
                                   os.environ.get("QL_PASSWORD", ""))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, X-Hermes-Password")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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

    def _is_models_hide(self, path):
        return path.endswith("/hermes/models/hide")

    def _is_inspect_probe(self, path):
        return path.endswith("/hermes/inspect/probe")

    def _is_inspect_memory(self, path):
        return path.endswith("/hermes/inspect/memory")

    def _is_inspect_config(self, path):
        return path.endswith("/hermes/inspect/config")

    def _is_inspect_hermes_models(self, path):
        return path.endswith("/hermes/inspect/models")

    def _is_ideas(self, path):
        return path.endswith("/agent/ideas")

    def _is_suggestions(self, path):
        return path.endswith("/agent/suggestions")

    def _is_feed_prompt(self, path):
        return path.endswith("/agent/feed/prompt")

    def _is_agent_settings(self, path):
        return path.endswith("/agent/settings")

    def _is_providers(self, path):
        return path.endswith("/hermes/providers")

    def _is_provider_item(self, path):
        # DELETE /api/hermes/providers/{id}
        parts = path.rstrip("/").split("/")
        return len(parts) >= 2 and parts[-2] == "providers" and parts[-1]

    def _provider_item_id(self, path):
        return path.rstrip("/").split("/")[-1]

    def _is_platforms(self, path):
        return path.endswith("/hermes/platforms")

    def _is_skills(self, path):
        return path.endswith("/hermes/skills") or path.endswith("/agent/skills")

    def _is_oauth_vendors(self, path):
        return (path.endswith("/hermes/oauth/vendors")
                or path.endswith("/agent/oauth/vendors"))

    def _is_oauth_start(self, path):
        return (path.endswith("/hermes/oauth/start")
                or path.endswith("/agent/oauth/start"))

    def _is_oauth_callback(self, path):
        return (path.endswith("/hermes/oauth/callback")
                or path.endswith("/agent/oauth/callback"))

    def _is_oauth_disconnect(self, path):
        return (path.endswith("/hermes/oauth/disconnect")
                or path.endswith("/agent/oauth/disconnect"))

    # Wave 3 收尾（agent_prefs.py）：资讯点赞 / 动作权限 / 产物沉淀 /
    # 媒体生成 / 简报口径。统一走 /api/agent 前缀（单后端：App 不本地存）。
    def _is_brief_like(self, path):
        return path.endswith("/agent/brief/like")

    def _is_brief_likes(self, path):
        return path.endswith("/agent/brief/likes")

    def _is_action_policy(self, path):
        return path.endswith("/agent/action-policy")

    def _is_artifacts(self, path):
        return path.endswith("/agent/artifacts")

    def _is_media_generate(self, path):
        return path.endswith("/agent/media/generate")

    def _is_brief(self, path):
        return path.endswith("/agent/brief")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        # OAuth 浏览器回调：免鉴权（浏览器带不上鉴权头），靠 state 防 CSRF；
        # /start 本身要求鉴权，故 state 只有登录用户能签发。
        if self._is_oauth_callback(path):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (qs.get("code") or [""])[0]
            state = (qs.get("state") or [""])[0]
            ok, title, msg = hermes_oauth.handle_callback(code, state)
            self._send_html(200, hermes_oauth.callback_page(ok, title, msg))
            return
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        if self._is_upstream(path):
            key = hermes_upstream.get_key()
            self._send(200, {
                "url": hermes_upstream.get_base_url(),
                "has_key": bool(key),
            })
            return
        # v4.4.x：经 docker socket 读 Hermes 容器（完整同步：模型/服务商/记忆/技能）
        if self._is_inspect_probe(path):
            import hermes_inspect
            self._send(200, hermes_inspect.probe())
            return
        if self._is_inspect_memory(path):
            import hermes_inspect
            mem = hermes_inspect.get_hermes_memory()
            self._send(200, {"memory": mem, "count": len(mem)})
            return
        if self._is_inspect_config(path):
            import hermes_inspect
            ok, cfg = hermes_inspect.get_hermes_config()
            # 不返回完整 config（可能含密钥），只返回顶层结构摘要
            summary = {}
            if ok and isinstance(cfg, dict):
                for k, v in cfg.items():
                    if isinstance(v, dict):
                        summary[k] = {"type": "dict", "keys": list(v.keys())[:20]}
                    elif isinstance(v, list):
                        summary[k] = {"type": "list", "count": len(v)}
                    else:
                        s = str(v)
                        # 脱敏：可能是 key/token 的值不返回原文
                        if any(w in k.lower() for w in ("key", "token", "secret", "password")):
                            s = "***" if s else ""
                        elif len(s) > 80:
                            s = s[:80] + "…"
                        summary[k] = s
            self._send(200, {"ok": ok, "structure": summary})
            return
        if self._is_inspect_hermes_models(path):
            import hermes_inspect
            ok, models = hermes_inspect.get_hermes_models()
            selected_ok, selected = hermes_inspect.get_selected_hermes_model()
            if selected:
                for model in models:
                    model["selected"] = (model.get("id") == selected["id"]
                                         and model.get("provider") == selected["provider"])
            self._send(200, {"ok": bool(ok and selected_ok), "models": models,
                             "selected": selected, "count": len(models)})
            return
        # v4.4.x：AI 内容生成（点子/今日建议）——提示词后端统一管，iOS 只展示
        if self._is_ideas(path):
            import ai_content
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            force = (qs.get("force") or [""])[0] == "1"
            self._send(200, ai_content.get_ideas(force=force))
            return
        if self._is_suggestions(path):
            import ai_content
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            force = (qs.get("force") or [""])[0] == "1"
            self._send(200, ai_content.get_suggestions(force=force))
            return
        # v4.4.x：Feed prompt 存后端
        if self._is_feed_prompt(path):
            import feed_prefs
            if self.command == "GET":
                self._send(200, {"prompt": feed_prefs.get_prompt()})
            elif self.command == "POST":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    prompt = str(body.get("prompt", ""))
                    ok = feed_prefs.set_prompt(prompt)
                    self._send(200, {"ok": ok, "prompt": prompt})
                except Exception as e:
                    self._send(400, {"ok": False, "error": str(e)[:100]})
            else:
                self._send(405, {"error": "method not allowed"})
            return
        # v4.4.x：通用设置存后端（上下文压缩等）
        if self._is_agent_settings(path):
            import agent_settings
            if self.command == "GET":
                self._send(200, {"settings": agent_settings.get_settings()})
            elif self.command == "POST":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    ok = True
                    for k, v in body.items():
                        if not agent_settings.set_setting(k, v):
                            ok = False
                    self._send(200, {"ok": ok})
                except Exception as e:
                    self._send(400, {"ok": False, "error": str(e)[:100]})
            else:
                self._send(405, {"error": "method not allowed"})
            return
        if self._is_models(path):
            sel = hermes_upstream.get_selected()
            out = []
            for grp in hermes_upstream.get_all_models():
                models = grp["models"]
                for m in models:
                    m["selected"] = (m["id"] == sel["model"]
                                     and grp["id"] == sel["provider"])
                out.append({"id": grp["id"], "name": grp["name"],
                            "models": models, "error": grp["error"]})
            self._send(200, {"providers": out})
            return
        if self._is_providers(path):
            self._send(200, {"providers": hermes_upstream.get_providers()})
            return
        if self._is_platforms(path):
            self._send(200, {"platforms": hermes_platforms.get_platforms()})
            return
        if self._is_skills(path):
            self._send(200, {"skills": hermes_skills.list_skills()})
            return
        if self._is_oauth_vendors(path):
            self._send(200, {"vendors": hermes_oauth.list_vendors()})
            return
        if self._is_brief_likes(path):
            self._send(200, {"liked_ids": agent_prefs.get_liked_ids()})
            return
        if self._is_action_policy(path):
            self._send(200, {"policy": agent_prefs.get_policy()})
            return
        if self._is_artifacts(path):
            self._send(200, {"artifacts": agent_prefs.list_artifacts()})
            return
        if self._is_brief(path):
            self._send(200, {"brief": agent_prefs.get_brief(),
                             "status": "not_configured"})
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
            pid = str(body.get("provider", "") or "")
            # 2026-10-07：切换必须写 Hermes 配置（之前只写后端自己的配置，两边脱节）
            import hermes_inspect
            ok, msg = hermes_inspect.set_hermes_model(mid, pid or None)
            if not ok:
                self._send(200, {"ok": False, "error": "切换 Hermes 模型失败: %s" % msg})
                return
            if not hermes_upstream.save_selected_model(mid, pid or None)[0]:
                self._send(200, {"ok": False, "error": "Hermes 已切换，但后端兼容配置未同步，请重试确认"})
                return
            # Keep the compatibility cache aligned with Hermes before confirming success.
            self._send(200, {"ok": True})
            return
        if self._is_models_hide(path):
            pid = str(body.get("provider", "") or "")
            ids = body.get("model_ids")
            ok, err = hermes_upstream.save_hidden_models(
                pid, ids if isinstance(ids, list) else [])
            if not ok:
                self._send(200, {"ok": False, "error": err})
                return
            self._send(200, {"ok": True})
            return
        if self._is_providers(path):
            ok, payload = hermes_upstream.add_provider(
                body.get("name"), body.get("url"), body.get("key"))
            if not ok:
                self._send(200, {"ok": False, "error": payload})
                return
            self._send(200, {"ok": True, "provider": payload})
            return
        if self._is_platforms(path):
            pid = str(body.get("platform", "") or "")
            ok, err, restarted = hermes_platforms.set_platform(
                pid, body.get("enabled"), body.get("config"))
            if not ok:
                self._send(200, {"ok": False, "error": err})
                return
            self._send(200, {"ok": True,
                             "restart": "triggered" if restarted else "failed"})
            return
        if self._is_skills(path):
            sid = str(body.get("skill_id", "") or "")
            ok, err, restarted = hermes_skills.set_skill(sid, body.get("enabled"))
            if not ok:
                self._send(200, {"ok": False, "error": err})
                return
            if not restarted:
                self._send(200, {"ok": False, "error": "技能配置已保存，但 Hermes 网关重启失败",
                                 "restart": "failed", "saved": True})
                return
            self._send(200, {"ok": True,
                             "restart": "triggered"})
            return
        if self._is_suggestions(path):
            # 2026-10-07：健康 AI 联动 —— 接收 iOS 发来的健康摘要，喂给 Hermes 生成个性化建议
            import ai_content
            health = str(body.get("health") or "").strip()[:2000]
            self._send(200, ai_content.get_suggestions(force=True, health_context=health or None))
            return
        if self._is_oauth_start(path):
            vid = str(body.get("vendor_id", "") or "")
            host = self.headers.get("Host", "")
            ok, payload = hermes_oauth.start_flow(vid, host)
            if not ok:
                self._send(200, {"ok": False, **payload})
                return
            self._send(200, {"ok": True, **payload})
            return
        if self._is_oauth_disconnect(path):
            vid = str(body.get("vendor_id", "") or "")
            ok = hermes_oauth.disconnect(vid)
            self._send(200, {"ok": ok} if ok else
                       {"ok": False, "error": "未知厂商：%s" % vid})
            return
        if self._is_brief_like(path):
            ok, err = agent_prefs.set_like(body.get("article_id"),
                                           body.get("liked"))
            self._send(200, {"ok": True} if ok else
                       {"ok": False, "error": err})
            return
        if self._is_action_policy(path):
            p = body.get("policy")
            ok, err, policy = agent_prefs.set_policy(
                p if isinstance(p, dict) else None)
            self._send(200, {"ok": True, "policy": policy} if ok else
                       {"ok": False, "error": err})
            return
        if self._is_artifacts(path):
            ok, err, art = agent_prefs.create_artifact(
                body.get("title"), body.get("kind"), body.get("content"))
            self._send(200, {"ok": True, "artifact": art} if ok else
                       {"ok": False, "error": err})
            return
        if self._is_media_generate(path):
            prompt = str(body.get("prompt", "") or "").strip()
            if not prompt:
                self._send(200, {"ok": False, "error": "缺少 prompt"})
                return
            code, payload = agent_prefs.generate_media(prompt)
            self._send(code, payload)  # v1 如实 501，绝不伪造图片
            return
        if self._is_brief(path):
            ok, err = agent_prefs.set_brief(body.get("brief"))
            if not ok:
                self._send(200, {"ok": False, "error": err})
                return
            # v1：只存用户口径；真正的 agent 主笔简报需要内容源管线（后续专项），
            # 此处不编造文章。
            self._send(200, {"status": "not_configured", "articles": []})
            return
        self._send(404, {"error": "Not Found"})

    def do_DELETE(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:  # noqa: BLE001
            body = {}
        if self._is_provider_item(path):
            pid = self._provider_item_id(path)
            ok, err = hermes_upstream.delete_provider(pid)
            self._send(200, {"ok": True} if ok else
                       {"ok": False, "error": err})
            return
        if self._is_artifacts(path):
            if agent_prefs.delete_artifact(body.get("id")):
                self._send(200, {"ok": True})
            else:
                self._send(200, {"ok": False, "error": "not_found"})
            return
        self._send(404, {"error": "Not Found"})
