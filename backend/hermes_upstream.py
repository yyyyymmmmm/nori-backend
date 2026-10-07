#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes 上游（upstream）统一解析：多服务商。

v4.4.x 起支持多个 Hermes 上游（DeepSeek/OpenAI/自建…），App 按服务商分组选模型。

数据（{STREAM_DATA_DIR}/hermes_upstream.json）：
  {
    "hermes_upstreams": [{"id": "abc123", "name": "DeepSeek", "url": "...", "key": "..."}],
    "selected_provider": "abc123",      # App 选中的服务商 id（默认 "default"）
    "model_id": "deepseek-chat",        # 选中的模型 id（兼容老单字段）
    "model_provider": "abc123",         # 模型所属服务商（老数据缺省→"default"）
    "hidden_models": {"abc123": ["m1"]},# 隐藏的模型黑名单
    # —— 以下为老版本遗留（无 hermes_upstreams 时兼容） ——
    "url": "...", "key": "..."
  }

取值优先级（default 服务商）：
  URL：STREAM_HERMES_URL → 持久化 hermes_upstreams[default].url
       → 老 url 字段 → http://127.0.0.1:9123
  Key：STREAM_HERMES_KEY → 持久化 key → ""

ENV 指定的 default 服务商不可编辑/删除（服务端内部配置，App 只读）；
用户自加的服务商可增删改。

只依赖标准库。
"""
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_BASE_URL = "http://127.0.0.1:9123"
_CONFIG_NAME = "hermes_upstream.json"
_KNOWN_SUFFIXES = ("/v1/chat/completions", "/v1/responses")
_DEFAULT_ID = "default"

_lock = threading.Lock()
# 模型缓存：按服务商 id 分桶 {pid: {"ts":..., "models":..., "error":...}}
_models_cache = {}


def _data_dir():
    return os.environ.get("STREAM_DATA_DIR", "/data/streams_data")


def _config_path():
    return os.path.join(_data_dir(), _CONFIG_NAME)


def _load_persisted():
    try:
        with open(_config_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def _normalize_url(url):
    """清洗为基地址（如 http://192.168.1.5:9123）；非法返回 ""。"""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u
    scheme = u.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        return ""
    return u


def _strip_known_suffixes(u):
    for s in _KNOWN_SUFFIXES:
        if u.endswith(s):
            return u[: -len(s)]
    return u


# ────────────────────────── 服务商解析 ──────────────────────────

def get_providers():
    """返回 [{id, name, url, has_key, is_default, editable}]（key 永不返回）。"""
    persisted = _load_persisted()
    providers = []
    env_url = _normalize_url(os.environ.get("STREAM_HERMES_URL", ""))
    env_key = os.environ.get("STREAM_HERMES_KEY", "")
    if env_url:
        # ENV 指定的 default：最高优先，不可编辑/删除
        providers.append({
            "id": _DEFAULT_ID, "name": "默认",
            "url": _strip_known_suffixes(env_url),
            "has_key": bool(env_key),
            "is_default": True, "editable": False,
        })
    seen = {p["id"] for p in providers}
    for u in persisted.get("hermes_upstreams") or []:
        if not isinstance(u, dict):
            continue
        pid = str(u.get("id") or "").strip()
        url = _normalize_url(u.get("url"))
        if not pid or not url or pid in seen:
            continue
        seen.add(pid)
        providers.append({
            "id": pid, "name": str(u.get("name") or pid),
            "url": _strip_known_suffixes(url),
            "has_key": bool(u.get("key")),
            "is_default": pid == _DEFAULT_ID, "editable": pid != _DEFAULT_ID,
        })
    # 兼容：老单组持久化（无 hermes_upstreams 且无 ENV）
    if not providers:
        old_url = _normalize_url(persisted.get("url", ""))
        if old_url:
            providers.append({
                "id": _DEFAULT_ID, "name": "默认", "url": old_url,
                "has_key": bool(persisted.get("key")),
                "is_default": True, "editable": True,
            })
    if not providers:
        providers.append({
            "id": _DEFAULT_ID, "name": "默认", "url": DEFAULT_BASE_URL,
            "has_key": False, "is_default": True, "editable": True,
        })
    return providers


def _provider_secret(pid):
    """取某服务商的 {url, key}（内部用，key 可返回）。"""
    pid = str(pid or _DEFAULT_ID)
    persisted = _load_persisted()
    env_url = _normalize_url(os.environ.get("STREAM_HERMES_URL", ""))
    if pid == _DEFAULT_ID and env_url:
        return {"url": _strip_known_suffixes(env_url),
                "key": os.environ.get("STREAM_HERMES_KEY", "")}
    for u in persisted.get("hermes_upstreams") or []:
        if isinstance(u, dict) and str(u.get("id")) == pid:
            url = _normalize_url(u.get("url"))
            if url:
                return {"url": _strip_known_suffixes(url),
                        "key": str(u.get("key") or "")}
    if pid == _DEFAULT_ID:
        # 老单组持久化
        url = _normalize_url(persisted.get("url", ""))
        if url:
            return {"url": url, "key": str(persisted.get("key") or "")}
        return {"url": DEFAULT_BASE_URL, "key": ""}
    return {"url": "", "key": ""}


def get_selected_provider_id():
    """App 选中的服务商 id；非法/缺失回退 default。"""
    pid = str(_load_persisted().get("selected_provider") or _DEFAULT_ID).strip()
    ids = {p["id"] for p in get_providers()}
    return pid if pid in ids else _DEFAULT_ID


# ────────────────────────── 兼容旧单服务商语义 ──────────────────────────
# 以下函数保持旧签名，走"选中服务商"（未选= default），供各模块无感使用。

def get_base_url():
    return _provider_secret(get_selected_provider_id())["url"] or DEFAULT_BASE_URL


def get_key():
    return _provider_secret(get_selected_provider_id())["key"]


def chat_completions_url():
    return get_base_url() + "/v1/chat/completions"


def responses_url():
    ov = _normalize_url(os.environ.get("STREAM_HERMES_RESPONSES_URL", ""))
    if ov:
        return ov
    return get_base_url() + "/v1/responses"


def health_url():
    return get_base_url() + "/health"


def models_url():
    return get_base_url() + "/v1/models"


def model_options_url():
    return get_base_url() + "/api/model/options"


def _auth_headers(key):
    h = {}
    if key:
        h["Authorization"] = "Bearer " + key
    return h


def test_upstream(url, key, timeout=10):
    """实测上游连通性。返回 (ok: bool, error_zh: str)。"""
    base = _normalize_url(url)
    if not base:
        return False, "地址格式不正确（示例：http://192.168.1.5:9123）"
    key = (key or "").strip()
    last_err = ""
    for path in ("/health", "/v1/models"):
        try:
            req = urllib.request.Request(base + path, headers=_auth_headers(key))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return True, ""
                last_err = "Hermes 返回异常状态码：HTTP %d" % resp.status
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "鉴权失败（401），请检查密钥是否正确"
            last_err = "Hermes 返回异常状态码：HTTP %d" % e.code
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e) or "")
            if "refused" in reason.lower():
                last_err = "连接被拒绝，请检查地址和端口是否正确"
            else:
                last_err = "无法连接到 Hermes：%s" % reason[:80]
        except TimeoutError:
            last_err = "连接超时（%d 秒），请检查地址是否可达" % timeout
        except Exception as e:  # noqa: BLE001
            last_err = "连接失败：%s" % str(e)[:80]
    return False, last_err or "连接失败"


def _persist_merge(updates):
    """合并落盘（原子写，0600；保留已有字段）。返回 (ok: bool, error_zh: str)。"""
    d = _load_persisted()
    d.update(updates)
    data_dir = _data_dir()
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as e:
        return False, "无法创建数据目录：%s" % e
    path = _config_path()
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".hermes_upstream.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        return False, "保存失败：%s" % e
    return True, ""


def _bust_models_cache(pid=None):
    with _lock:
        if pid:
            _models_cache.pop(pid, None)
        else:
            _models_cache.clear()


def save_upstream(url, key):
    """落盘 default 上游（兼容老 POST /api/hermes/upstream；ENV 模式下拒绝）。"""
    base = _normalize_url(url)
    if not base:
        return False, "地址格式不正确（示例：http://192.168.1.5:9123）"
    if os.environ.get("STREAM_HERMES_URL", "").strip():
        return False, "当前上游由服务端配置（STREAM_HERMES_URL）接管，App 内不可改"
    key = (key or "").strip()
    d = _load_persisted()
    ups = [u for u in (d.get("hermes_upstreams") or []) if isinstance(u, dict)]
    found = False
    for u in ups:
        if str(u.get("id")) == _DEFAULT_ID:
            u["url"] = base
            u["key"] = key
            found = True
    if not found:
        ups.append({"id": _DEFAULT_ID, "name": "默认", "url": base, "key": key})
    ok, err = _persist_merge({"hermes_upstreams": ups})
    if ok:
        _bust_models_cache(_DEFAULT_ID)
    return ok, err


# ────────────────────────── 服务商增删 ──────────────────────────

def add_provider(name, url, key):
    """新增用户服务商：先实测连通再落盘。返回 (ok, error_zh / provider)。"""
    name = str(name or "").strip() or "未命名"
    base = _normalize_url(url)
    if not base:
        return False, "地址格式不正确（示例：http://192.168.1.5:9123）"
    key = str(key or "").strip()
    ok, err = test_upstream(base, key)
    if not ok:
        return False, err
    pid = uuid.uuid4().hex[:8]
    d = _load_persisted()
    ups = [u for u in (d.get("hermes_upstreams") or []) if isinstance(u, dict)]
    ups.append({"id": pid, "name": name, "url": base, "key": key})
    ok2, err2 = _persist_merge({"hermes_upstreams": ups})
    if not ok2:
        return False, err2
    return True, {"id": pid, "name": name, "url": base, "has_key": bool(key)}


def delete_provider(pid):
    """删除用户服务商；default 不许删。返回 (ok, error_zh)。"""
    pid = str(pid or "").strip()
    if pid == _DEFAULT_ID:
        return False, "默认服务商不可删除"
    d = _load_persisted()
    ups = [u for u in (d.get("hermes_upstreams") or [])
           if isinstance(u, dict) and str(u.get("id")) != pid]
    if len(ups) == len(d.get("hermes_upstreams") or []):
        return False, "服务商不存在"
    updates = {"hermes_upstreams": ups}
    # 删的是当前选中 → 回退 default
    if str(d.get("selected_provider") or "") == pid:
        updates["selected_provider"] = _DEFAULT_ID
        updates["model_id"] = ""
        updates["model_provider"] = _DEFAULT_ID
    ok, err = _persist_merge(updates)
    if ok:
        _bust_models_cache(pid)
    return ok, err


def select_provider(pid):
    """切换当前服务商。返回 (ok, error_zh)。"""
    pid = str(pid or "").strip()
    if pid not in {p["id"] for p in get_providers()}:
        return False, "服务商不存在"
    return _persist_merge({"selected_provider": pid})


# ────────────────────────── 模型选择（含隐藏） ──────────────────────────

def get_selected_model():
    """App 选中的 Hermes 模型 id；从未选过返回 ""。"""
    return str(_load_persisted().get("model_id") or "").strip()


def get_selected():
    """返回 {"provider": pid, "model": mid}（老数据 model_provider 缺省→default）。"""
    d = _load_persisted()
    mid = str(d.get("model_id") or "").strip()
    pid = str(d.get("model_provider") or _DEFAULT_ID).strip()
    if pid not in {p["id"] for p in get_providers()}:
        pid = _DEFAULT_ID
    return {"provider": pid, "model": mid}


def effective_model():
    """Hermes 请求实际要带的 model：选中返回其 id，未选中返回 None。"""
    m = get_selected_model()
    return m or None


def effective_provider_id():
    """当前请求应路由到的服务商 id。"""
    return get_selected()["provider"]


def get_hidden_models():
    """返回 {provider_id: [model_id]} 黑名单。"""
    d = _load_persisted().get("hidden_models")
    if not isinstance(d, dict):
        return {}
    return {str(k): [str(x) for x in v] for k, v in d.items()
            if isinstance(v, list)}


def save_hidden_models(provider, model_ids):
    """整体替换某服务商的隐藏名单。返回 (ok, error_zh)。"""
    provider = str(provider or "").strip()
    if not provider:
        return False, "服务商不存在"
    hidden = get_hidden_models()
    ids = [str(x).strip() for x in (model_ids or []) if str(x).strip()]
    if ids:
        hidden[provider] = ids
    else:
        hidden.pop(provider, None)
    return _persist_merge({"hidden_models": hidden})


def save_selected_model(model_id, provider=None):
    """校验并落盘模型选择。返回 (ok: bool, error_zh: str)。

    model_id 必须出现在指定（或当前选中）服务商的实时模型列表中，否则拒绝。
    """
    mid = str(model_id or "").strip()
    if not mid:
        return False, "请选择要使用的模型"
    pid = str(provider or "").strip() or get_selected_provider_id()
    if pid not in {p["id"] for p in get_providers()}:
        return False, "服务商不存在"
    models, err = get_provider_models(pid)
    if err:
        return False, err
    if mid not in {m["id"] for m in models}:
        return False, "该模型在该服务商不可用：%s" % mid
    return _persist_merge({"model_id": mid, "model_provider": pid,
                           "selected_provider": pid})


# ────────────────────────── 模型列表（按服务商） ──────────────────────────

def _parse_models_v1(data):
    out = []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return out
    for it in items:
        if isinstance(it, dict) and it.get("id"):
            mid = str(it["id"])
            out.append({"id": mid, "name": str(it.get("name") or it.get("label") or mid)})
        elif isinstance(it, str) and it:
            out.append({"id": it, "name": it})
    return out


def _parse_models_options(data):
    out = []
    items = None
    if isinstance(data, dict):
        for k in ("models", "data", "options"):
            if isinstance(data.get(k), list):
                items = data[k]
                break
    elif isinstance(data, list):
        items = data
    if not items:
        return out
    for it in items:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("model") or it.get("name")
            if mid:
                mid = str(mid)
                out.append({"id": mid, "name": str(it.get("name") or it.get("label") or mid)})
        elif isinstance(it, str) and it:
            out.append({"id": it, "name": it})
    return out


def _fetch_provider_models(pid, url, key, timeout=10):
    """拉单个服务商的模型列表。返回 (models, error_zh|None)。"""
    models, err = [], None
    try:
        req = urllib.request.Request(url + "/v1/models", headers=_auth_headers(key))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        models = _parse_models_v1(data)
        if not models:
            req2 = urllib.request.Request(url + "/api/model/options",
                                          headers=_auth_headers(key))
            with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                data2 = json.loads(resp2.read().decode("utf-8", "replace"))
            models = _parse_models_options(data2)
    except urllib.error.HTTPError as e:
        err = "鉴权失败（401），请检查密钥" if e.code == 401 \
            else "返回异常状态码：HTTP %d" % e.code
    except urllib.error.URLError:
        err = "无法连接（%s）" % url
    except TimeoutError:
        err = "连接超时"
    except Exception as e:  # noqa: BLE001
        err = "获取失败：%s" % str(e)[:80]
    if err is None and not models:
        err = "未返回可用模型"
    return models, err


def get_provider_models(pid, timeout=10):
    """单个服务商的模型列表（60 秒缓存，已过滤隐藏）。返回 (models, error|None)。"""
    pid = str(pid or _DEFAULT_ID)
    now = time.time()
    with _lock:
        c = _models_cache.get(pid)
        if c and c["models"] is not None and now - c["ts"] < 60:
            return c["models"], c["error"]
    sec = _provider_secret(pid)
    if not sec["url"]:
        return [], "服务商不存在"
    models, err = _fetch_provider_models(pid, sec["url"], sec["key"], timeout)
    if not err:
        hidden = set(get_hidden_models().get(pid, []))
        if hidden:
            models = [m for m in models if m["id"] not in hidden]
    with _lock:
        _models_cache[pid] = {"ts": now, "models": models, "error": err}
    return models, err


def get_all_models(timeout=10):
    """全部服务商的模型列表。返回 [{id, name, models: [...], error}]。

    单家挂了只标 error，不影响其他家。
    """
    out = []
    for p in get_providers():
        models, err = get_provider_models(p["id"], timeout=timeout)
        out.append({"id": p["id"], "name": p["name"],
                    "models": models, "error": err})
    return out


def get_models(timeout=10):
    """兼容旧签名：返回当前选中服务商的模型列表。"""
    return get_provider_models(get_selected_provider_id(), timeout=timeout)
