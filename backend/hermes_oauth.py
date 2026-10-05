#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云服务连接器 OAuth 授权框架（MCP-first，点击授权）。

产品形态（对标 TodayAI 连接器页）：用户点「连接」→ 跳厂商授权页 → 点允许 →
浏览器回调本后端换 token → App 显示「已连接」。用户全程不填 URL/Token，
token 托管与自动续期都在后端；自定义 MCP 填 URL 仍保留给高级用户（mcp_api）。

端点（hermes_api.Handler 内挂载；另有 /api/agent/oauth/* 别名，
借 /api/agent 前缀 → lucky 白名单/relay/nginx 三处零改动）：
  GET  /api/hermes/oauth/vendors              厂商清单：id/name/capabilities/connected/icon
  POST /api/hermes/oauth/start   {vendor_id}  → {"auth_url"}（未配凭证时报 oauth_not_configured）
  GET  /api/hermes/oauth/callback?code&state  浏览器回调：code 换 token 并落盘，
                                              返回「已完成，请返回 App」HTML（此接口免鉴权，
                                              靠 state 防 CSRF；/start 本身要求鉴权）
  POST /api/hermes/oauth/disconnect {vendor_id} → {"ok": true}

厂商开发者凭证（环境变量，后端启动时注入）：
  QL_OAUTH_<VENDOR>_CLIENT_ID / QL_OAUTH_<VENDOR>_CLIENT_SECRET
  （VENDOR 为大写 vendor_id，如 QL_OAUTH_FEISHU_CLIENT_ID）
  商业版：由产品方统一注册、内置。 自托管（NAS/Docker）用户：
  去对应厂商开放平台注册一次应用，拿到 Client ID/Secret 后填进后端
  环境变量并重启后端即可。分厂商注册入口见 README「连接器 OAuth 配置」。

回调地址：QL_OAUTH_REDIRECT_BASE（如 https://xxx.lucky.com）；未设时
用 /start 请求的 Host 头推导（App 与浏览器走同一入口时成立）。

Token 落盘 {STREAM_DATA_DIR}/oauth_tokens.json（原子写，0600），token 本身
永不经 API 返回。过期前 300 秒用 refresh_token 自动续期，对调用方透明。

诚实说明（deferred，待真实凭证联调）：
  - 各厂商 authorize/token 端点按公开文档整理，飞书/钉钉/百度网盘为标准
    OAuth2；企业微信第一版走应用级 token（corpid/corpsecret 直换，
    可发消息/调接口），成员扫码绑定后续版本补；腾讯文档端点待与开放平台
    文档核对。
  - 框架本身（state/换 token/落盘/刷新/断开）已用本地 mock 厂商端到端自测，
    见 hermes_oauth_test.py。

只依赖标准库。
"""
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
import urllib.request

_lock = threading.Lock()

_DATA_NAME = "oauth_tokens.json"
_STATE_TTL = 600          # state 有效期 10 分钟
_REFRESH_MARGIN = 300     # 过期前 300 秒尝试续期

# state -> (vendor_id, expires_at)，内存保存；重启后进行中的授权需重新点「连接」
_pending_states = {}


def _data_dir():
    return os.environ.get("STREAM_DATA_DIR", "/data/streams_data")


def _tokens_path():
    return os.path.join(_data_dir(), _DATA_NAME)


# ---------------------------------------------------------------------------
# 厂商规格
# ---------------------------------------------------------------------------
# 字段：
#   name/capabilities/icon   App 展示用
#   authorize_url            授权页
#   authorize_params         拼到授权 URL 的固定参数（client_id/redirect_uri/state 由框架填）
#   token_url/token_method   换 token：POST（form/json）或 GET
#   token_body               POST 时的 body 格式
#   id_param/secret_param    凭证参数名（飞书用 app_id/app_secret）
#   token_extra              换 token 时的固定附加参数
#   refresh_url              续期端点（缺省=token_url）；refresh_grant 续期 grant_type
#   token_params             GET 换 token 时的参数模板，{client_id}/{client_secret} 占位
VENDORS = {
    "feishu": {
        "name": "飞书",
        "capabilities": "文档、日历、消息",
        "icon": "📘",
        "authorize_url": "https://open.feishu.cn/open-apis/authen/v1/authorize",
        "authorize_params": {"scope": "contact:user.base:readonly drive:drive"},
        "token_url": "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
        "token_method": "POST",
        "token_body": "json",
        "id_param": "app_id",
        "secret_param": "app_secret",
        "token_extra": {"grant_type": "authorization_code"},
        "refresh_url": "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token",
        "refresh_grant": "refresh_token",
    },
    "dingtalk": {
        "name": "钉钉",
        "capabilities": "审批、消息、日历",
        "icon": "🔵",
        "authorize_url": "https://login.dingtalk.com/oauth2/auth",
        "authorize_params": {"response_type": "code", "scope": "openid", "prompt": "consent"},
        "token_url": "https://api.dingtalk.com/v1.0/oauth2/userAccessToken",
        "token_method": "POST",
        "token_body": "json",
        "id_param": "clientId",
        "secret_param": "clientSecret",
        "token_extra": {"grantType": "authorization_code"},
        "refresh_url": "https://api.dingtalk.com/v1.0/oauth2/refreshToken",
        "refresh_grant": "refresh_token",
    },
    "wecom": {
        # 第一版：应用级 token（corpid/corpsecret 直换 gettoken），可发消息/调接口；
        # 成员扫码授权绑定后续版本补。/start 仍返回微信扫码授权页供将来扩展。
        "name": "企业微信",
        "capabilities": "消息、通讯录",
        "icon": "💼",
        "authorize_url": "https://open.weixin.qq.com/connect/oauth2/authorize",
        "authorize_params": {"response_type": "code", "scope": "snsapi_base"},
        "token_url": "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        "token_method": "GET",
        "id_param": "corpid",
        "secret_param": "corpsecret",
        "token_params": {"corpid": "{client_id}", "corpsecret": "{client_secret}"},
        "token_extra": {},
    },
    "tencent_docs": {
        # 端点待与腾讯文档开放平台文档核对（deferred）；框架先行。
        "name": "腾讯文档",
        "capabilities": "文档读写",
        "icon": "📄",
        "authorize_url": "https://docs.qq.com/oauth/v2/authorize",
        "authorize_params": {"response_type": "code", "scope": "docs:read docs:write"},
        "token_url": "https://docs.qq.com/oauth/v2/token",
        "token_method": "POST",
        "token_body": "form",
        "token_extra": {"grant_type": "authorization_code"},
    },
    "baidu_netdisk": {
        "name": "百度网盘",
        "capabilities": "文件存取",
        "icon": "☁️",
        "authorize_url": "https://openapi.baidu.com/oauth/2.0/authorize",
        "authorize_params": {"response_type": "code", "scope": "basic,netdisk"},
        "token_url": "https://openapi.baidu.com/oauth/2.0/token",
        "token_method": "POST",
        "token_body": "form",
        "token_extra": {"grant_type": "authorization_code"},
        "refresh_grant": "refresh_token",
    },
}


def _env_prefix(vendor_id):
    return "QL_OAUTH_" + str(vendor_id).upper()


def get_credentials(vendor_id):
    """读厂商开发者凭证；任一缺失返回 (None, None)。"""
    p = _env_prefix(vendor_id)
    cid = os.environ.get(p + "_CLIENT_ID", "").strip()
    sec = os.environ.get(p + "_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        return None, None
    return cid, sec


def is_configured(vendor_id):
    cid, sec = get_credentials(vendor_id)
    return bool(cid and sec)


# ---------------------------------------------------------------------------
# token 落盘（0600，原子写）
# ---------------------------------------------------------------------------
def _load_tokens():
    try:
        with open(_tokens_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def _save_tokens(d):
    data_dir = _data_dir()
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".oauth.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, _tokens_path())
    except OSError:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# HTTP（可 mock，便于自测）
# ---------------------------------------------------------------------------
def _http_request(url, method="POST", body_format="form", params=None, timeout=15):
    """发换 token/续期请求，返回解析后的 dict；失败抛 RuntimeError。"""
    params = params or {}
    try:
        if method == "GET":
            full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
            req = urllib.request.Request(full, method="GET")
            data = None
            headers = {}
        else:
            if body_format == "json":
                data = json.dumps(params).encode("utf-8")
                headers = {"Content-Type": "application/json"}
            else:
                data = urllib.parse.urlencode(params).encode("utf-8")
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("非 JSON 对象")
        return d
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("请求厂商 token 接口失败：%s" % exc)


def _extract_token(d):
    """从厂商返回里提 access/refresh/expires_in；含 error 时抛 RuntimeError。"""
    if not isinstance(d, dict):
        raise RuntimeError("厂商返回格式异常")
    err = d.get("error") or d.get("errcode")
    # 企业微信：errcode=0 才是成功
    if err and str(err) not in ("0",):
        raise RuntimeError("厂商拒绝：%s" % (d.get("error_description") or d.get("errmsg") or err))
    at = d.get("access_token") or ""
    if not at:
        raise RuntimeError("厂商返回缺少 access_token")
    try:
        expires_in = int(d.get("expires_in") or 7200)
    except (TypeError, ValueError):
        expires_in = 7200
    return {
        "access_token": at,
        "refresh_token": d.get("refresh_token") or "",
        "expires_at": int(time.time()) + max(expires_in, 60),
        "token_type": d.get("token_type") or "Bearer",
        "scope": d.get("scope") or "",
        "obtained_at": int(time.time()),
    }


def _exchange_code(spec, vendor_id, code, redirect_uri):
    """code → token 记录（含 expires_at）。"""
    cid, sec = get_credentials(vendor_id)
    method = spec.get("token_method", "POST")
    if method == "GET":
        tpl = spec.get("token_params", {})
        params = {k: v.replace("{client_id}", cid).replace("{client_secret}", sec)
                  for k, v in tpl.items()}
    else:
        params = {
            spec.get("id_param", "client_id"): cid,
            spec.get("secret_param", "client_secret"): sec,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        params.update(spec.get("token_extra", {}))
    d = _http_request(spec["token_url"], method=method,
                      body_format=spec.get("token_body", "form"), params=params)
    return _extract_token(d)


def _refresh(spec, vendor_id, rec):
    """用 refresh_token 续期；成功返回新记录，失败抛 RuntimeError。"""
    rt = rec.get("refresh_token") or ""
    if not rt:
        raise RuntimeError("无 refresh_token，需重新授权")
    cid, sec = get_credentials(vendor_id)
    url = spec.get("refresh_url") or spec["token_url"]
    params = {
        spec.get("id_param", "client_id"): cid,
        spec.get("secret_param", "client_secret"): sec,
        "grant_type": spec.get("refresh_grant", "refresh_token"),
        "refresh_token": rt,
    }
    d = _http_request(url, method="POST",
                      body_format=spec.get("token_body", "form"), params=params)
    new_rec = _extract_token(d)
    if not new_rec.get("refresh_token"):
        new_rec["refresh_token"] = rt  # 有些厂商续期不返回新的 refresh_token
    return new_rec


# ---------------------------------------------------------------------------
# 对外逻辑（hermes_api.Handler 是薄胶水）
# ---------------------------------------------------------------------------
def list_vendors():
    """App 连接器页用：每厂商 id/name/capabilities/connected/icon（token 永不返回）。"""
    out = []
    for vid, spec in VENDORS.items():
        out.append({
            "id": vid,
            "name": spec["name"],
            "capabilities": spec.get("capabilities", ""),
            "connected": is_connected(vid),
            "icon": spec.get("icon", ""),
        })
    return out


def _prune_states():
    now = time.time()
    for k in [k for k, (_, exp) in _pending_states.items() if exp < now]:
        _pending_states.pop(k, None)


def redirect_uri_for(host_header=""):
    """回调地址：环境变量优先，否则用请求 Host 头推导（https）。"""
    base = os.environ.get("QL_OAUTH_REDIRECT_BASE", "").strip().rstrip("/")
    if not base and host_header:
        host = host_header.split(",")[0].strip()
        if host:
            base = "https://" + host
    if not base:
        return ""
    return base + "/api/hermes/oauth/callback"


def start_flow(vendor_id, host_header=""):
    """生成授权 URL。返回 (ok, payload)：ok 时 payload={"auth_url"}；
    未配凭证时 payload={"error": "oauth_not_configured", "hint": ...}。"""
    vid = str(vendor_id or "").strip()
    spec = VENDORS.get(vid)
    if not spec:
        return False, {"error": "unknown_vendor", "hint": "未知厂商：%s" % vid}
    if not is_configured(vid):
        return False, {
            "error": "oauth_not_configured",
            "hint": ("该连接器需在厂商开放平台注册应用后才能使用。"
                     "商业版由我们内置好，开箱即用；自托管用户请去%s开放平台"
                     "注册应用，拿到 Client ID / Client Secret 后填入后端环境变量 "
                     "%s_CLIENT_ID / %s_CLIENT_SECRET 并重启后端（详见 README"
                     "「连接器 OAuth 配置」）。"
                     % (spec["name"], _env_prefix(vid), _env_prefix(vid))),
        }
    redirect_uri = redirect_uri_for(host_header)
    if not redirect_uri:
        return False, {"error": "no_redirect_base",
                       "hint": "无法确定回调地址：请设置 QL_OAUTH_REDIRECT_BASE 环境变量"}
    cid, _ = get_credentials(vid)
    state = secrets.token_urlsafe(24)
    with _lock:
        _prune_states()
        _pending_states[state] = (vid, time.time() + _STATE_TTL)
    q = {
        spec.get("id_param", "client_id"): cid,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    q.update(spec.get("authorize_params", {}))
    auth_url = spec["authorize_url"] + "?" + urllib.parse.urlencode(q)
    return True, {"auth_url": auth_url}


def handle_callback(code, state):
    """浏览器回调：校验 state → code 换 token → 落盘。
    返回 (ok: bool, title: str, message: str) 供 HTML 页展示。"""
    code = str(code or "").strip()
    state = str(state or "").strip()
    with _lock:
        _prune_states()
        item = _pending_states.pop(state, None)
    if not item:
        return False, "授权失败", "state 无效或已过期，请返回 App 重新点「连接」。"
    vid, _ = item
    spec = VENDORS.get(vid)
    if not spec:
        return False, "授权失败", "未知厂商。"
    if not code:
        return False, "授权失败", "厂商未返回授权码（可能您点了拒绝）。"
    redirect_uri = redirect_uri_for()
    try:
        rec = _exchange_code(spec, vid, code, redirect_uri)
    except RuntimeError as exc:
        return False, "授权失败", "换 token 失败：%s" % exc
    try:
        with _lock:
            d = _load_tokens()
            d[vid] = rec
            _save_tokens(d)
    except OSError as exc:
        return False, "授权失败", "token 保存失败：%s" % exc
    return True, "连接成功", "%s 已连接，请返回 App。" % spec["name"]


def get_access_token(vendor_id):
    """取有效 access_token；临期自动续期；无/失效返回 None（对调用方透明）。"""
    vid = str(vendor_id or "").strip()
    spec = VENDORS.get(vid)
    if not spec:
        return None
    with _lock:
        rec = _load_tokens().get(vid)
    if not rec or not rec.get("access_token"):
        return None
    if rec.get("expires_at", 0) - time.time() > _REFRESH_MARGIN:
        return rec["access_token"]
    # 临期或已过期：尝试续期
    try:
        new_rec = _refresh(spec, vid, rec)
    except RuntimeError:
        # 续期失败：旧 token 若还没过期就继续用，过期则视为断开
        if rec.get("expires_at", 0) > time.time():
            return rec["access_token"]
        return None
    try:
        with _lock:
            d = _load_tokens()
            d[vid] = new_rec
            _save_tokens(d)
    except OSError:
        pass
    return new_rec["access_token"]


def is_connected(vendor_id):
    """connected 判定：有未过期（或可续期）的 token。"""
    return get_access_token(vendor_id) is not None


def disconnect(vendor_id):
    """断开：删 token。返回 True（本就没连也算成功）。"""
    vid = str(vendor_id or "").strip()
    if vid not in VENDORS:
        return False
    with _lock:
        d = _load_tokens()
        d.pop(vid, None)
        try:
            _save_tokens(d)
        except OSError:
            return False
    return True


def callback_page(ok, title, message):
    """回调结果 HTML（浏览器看，用户语言）。"""
    color = "#1a7f37" if ok else "#b42318"
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title></head>
<body style="font-family:-apple-system,Helvetica,Arial,sans-serif;display:flex;
min-height:90vh;align-items:center;justify-content:center;background:#f7f3ec;margin:0">
<div style="text-align:center;padding:32px">
<div style="font-size:56px">%s</div>
<h2 style="color:%s">%s</h2>
<p style="color:#666">%s</p>
</div></body></html>""" % (title, "✅" if ok else "⚠️", color, title, message)
