#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes 上游（upstream）统一解析：持久化配置 > 环境变量 > 默认值。

App 设置页（POST /api/hermes/upstream）写入的配置落盘于
{STREAM_DATA_DIR}/hermes_upstream.json；各模块（stream_api / cron_api /
goal_module / goals_api）一律经本模块函数取值——动态读取，App 改完
设置即时生效，无需重启容器。

取值优先级：
  URL：持久化 url → STREAM_HERMES_URL（去掉 /v1/chat/completions 等已知后缀取基地址）
       → http://127.0.0.1:9123
  Key：持久化 key（"key" 字段存在即采用，空字符串 = 免鉴权）
       → STREAM_HERMES_KEY → QL_AGENT_KEY → ""

只依赖标准库。
"""
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:9123"
_CONFIG_NAME = "hermes_upstream.json"
_KNOWN_SUFFIXES = ("/v1/chat/completions", "/v1/responses")

_lock = threading.Lock()
_models_cache = {"ts": 0.0, "models": None, "error": None}


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


def get_base_url():
    p = _load_persisted()
    u = _normalize_url(p.get("url", ""))
    if u:
        return u
    u = _normalize_url(os.environ.get("STREAM_HERMES_URL", ""))
    if u:
        return _strip_known_suffixes(u)
    return DEFAULT_BASE_URL


def get_key():
    p = _load_persisted()
    if "key" in p:
        return p.get("key") or ""
    return os.environ.get("STREAM_HERMES_KEY", "") or os.environ.get("QL_AGENT_KEY", "") or ""


def chat_completions_url():
    return get_base_url() + "/v1/chat/completions"


def responses_url():
    # 沿用 stream_api 旧语义：STREAM_HERMES_RESPONSES_URL 显式覆盖优先
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


def save_upstream(url, key):
    """落盘上游配置（原子写，0600；保留已有 model_id 选择）。返回 (ok: bool, error_zh: str)。"""
    base = _normalize_url(url)
    if not base:
        return False, "地址格式不正确（示例：http://192.168.1.5:9123）"
    key = (key or "").strip()
    ok, err = _persist_merge({"url": base, "key": key})
    if ok:
        with _lock:
            _models_cache["ts"] = 0.0
            _models_cache["models"] = None
            _models_cache["error"] = None
    return ok, err


def get_selected_model():
    """App 选中的 Hermes 模型 id；从未选过返回 ""。"""
    return str(_load_persisted().get("model_id") or "").strip()


def effective_model():
    """Hermes 请求实际要带的 model：选中返回其 id，未选中返回 None。

    调用方收到 None 时**不带** model 覆盖，让 Hermes 用自身配置的模型；
    不回退 App 侧 st["model"] —— 模型只认 Hermes 一处配置。
    """
    m = get_selected_model()
    return m or None


def save_selected_model(model_id):
    """校验并落盘模型选择（保留已有 url/key）。返回 (ok: bool, error_zh: str)。

    model_id 必须出现在 Hermes 实时模型列表中，否则拒绝保存。
    """
    mid = str(model_id or "").strip()
    if not mid:
        return False, "请选择要使用的模型"
    models, err = get_models()
    if err:
        return False, err
    if mid not in {m["id"] for m in models}:
        return False, "该模型在 Hermes 上游不可用：%s" % mid
    return _persist_merge({"model_id": mid})


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
    # /api/model/options 形态不固定：尽力从常见结构里提取
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


def get_models(timeout=10):
    """代理 Hermes 模型列表（60 秒缓存）。返回 (models: list, error_zh: str|None)。"""
    now = time.time()
    with _lock:
        if _models_cache["models"] is not None and now - _models_cache["ts"] < 60:
            return _models_cache["models"], _models_cache["error"]
    key = get_key()
    models, err = [], None
    try:
        req = urllib.request.Request(models_url(), headers=_auth_headers(key))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        models = _parse_models_v1(data)
        if not models:
            # v1 无结果时回退 Hermes 专属端点
            req2 = urllib.request.Request(model_options_url(), headers=_auth_headers(key))
            with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                data2 = json.loads(resp2.read().decode("utf-8", "replace"))
            models = _parse_models_options(data2)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            err = "鉴权失败（401），请检查 Hermes 密钥"
        else:
            err = "Hermes 返回异常状态码：HTTP %d" % e.code
    except urllib.error.URLError:
        err = "无法连接到 Hermes（%s），请检查上游地址" % get_base_url()
    except TimeoutError:
        err = "连接 Hermes 超时"
    except Exception as e:  # noqa: BLE001
        err = "获取模型列表失败：%s" % str(e)[:80]
    if err is None and not models:
        err = "Hermes 未返回可用模型"
    with _lock:
        _models_cache["ts"] = now
        _models_cache["models"] = models
        _models_cache["error"] = err
    return models, err
