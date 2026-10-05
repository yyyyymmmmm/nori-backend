#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wave 3 收尾：Agent 个性化偏好（资讯点赞 / 动作权限 / 产物沉淀 / 媒体生成占位 / 简报口径）。

iOS Wave 3 已按本文件头部的契约写好 UI；此前后端缺失，iOS 被迫用
UserDefaults 本地降级，违反「单后端」纪律。本模块把它们补成真接口，
全部落盘 {STREAM_DATA_DIR} 下 0600 json 文件——App 是纯控制平面，
不另起本地状态。

端点（unified_router 挂载 /api/agent/* 前缀 → hermes_api.Handler，详见 hermes_api.py）：
  POST /api/agent/brief/like   {article_id, liked} → {ok}；点赞/取消点赞
  GET  /api/agent/brief/likes  → {liked_ids: [...]}
  GET  /api/agent/action-policy → {policy: {read_calendar: ask, ...}}
  POST /api/agent/action-policy {policy: {send_message: deny}} → {ok, policy}（部分更新）
  GET  /api/agent/artifacts    → {artifacts: [...]}（新→旧，上限 200）
  POST /api/agent/artifacts    {title, kind, content} → {ok, artifact}
  DELETE /api/agent/artifacts  {id} → {ok}
  POST /api/agent/media/generate {prompt} → 501（如实返回未配置/未实现，绝不伪造图片）
  GET  /api/agent/brief        → {brief, status}
  POST /api/agent/brief        {brief} → {status: not_configured, articles: []}（v1 占位）

只依赖标准库。
"""
import json
import os
import tempfile
import threading
import time
import uuid

_lock = threading.Lock()


def _data_dir():
    return os.environ.get("STREAM_DATA_DIR", "/data/streams_data")


def _load(name, default):
    try:
        with open(os.path.join(_data_dir(), name), encoding="utf-8") as f:
            d = json.load(f)
        return d
    except (OSError, ValueError):
        pass
    # 深拷贝一份默认，避免调用方改到模块级对象
    return json.loads(json.dumps(default))


def _save(name, data):
    """原子写 + 0600（沿用 hermes_oauth._save_tokens 的写法）。"""
    data_dir = _data_dir()
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".prefs.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(data_dir, name))
    except OSError:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 1. 资讯点赞
# ---------------------------------------------------------------------------
_LIKES_FILE = "brief_likes.json"


def get_liked_ids():
    d = _load(_LIKES_FILE, {"liked_ids": []})
    ids = d.get("liked_ids")
    if not isinstance(ids, list):
        return []
    return sorted({str(i) for i in ids if str(i).strip()})


def set_like(article_id, liked):
    aid = str(article_id or "").strip()
    if not aid:
        return False, "缺少 article_id"
    if len(aid) > 256:
        return False, "article_id 过长"
    if not isinstance(liked, bool):
        return False, "liked 须为 true/false"
    with _lock:
        ids = set(get_liked_ids())
        if liked:
            ids.add(aid)
        else:
            ids.discard(aid)
        _save(_LIKES_FILE, {"liked_ids": sorted(ids)})
    return True, ""


# ---------------------------------------------------------------------------
# 2. 动作权限
# ---------------------------------------------------------------------------
_POLICY_FILE = "action_policy.json"

# 动作键固定 7 类；iOS 按此渲染权限行。
ACTION_KEYS = (
    "read_calendar",   # 读日历
    "read_contacts",   # 读通讯录
    "send_message",    # 发消息（微信/TG/邮件等渠道外发）
    "web_search",      # 联网搜索
    "file_rw",         # 文件读写
    "device_control",  # 设备控制（家居/手机能力调用）
    "media_generate",  # 媒体生成
)
POLICY_VALUES = ("allow", "ask", "deny")
DEFAULT_POLICY = "ask"

# 架构注释（纪律）：删除类操作（delete/remove/destroy/clear 语义）后端
# 永远要求单独确认，不受本 policy 影响。policy 只管「读/写/发」类动作的
# 默认询问策略；任何不可逆破坏性动作不得被 policy 静默放行。


def get_policy():
    d = _load(_POLICY_FILE, {"policy": {}})
    stored = d.get("policy")
    if not isinstance(stored, dict):
        stored = {}
    return {k: (stored.get(k) if stored.get(k) in POLICY_VALUES else DEFAULT_POLICY)
            for k in ACTION_KEYS}


def set_policy(partial):
    """部分更新；未知键/非法值直接拒绝（ok=False），不落盘。"""
    if not isinstance(partial, dict):
        return False, "policy 须为对象", get_policy()
    for k in partial:
        if k not in ACTION_KEYS:
            return False, "未知动作：%s" % k, get_policy()
    for k, v in partial.items():
        if v not in POLICY_VALUES:
            return False, "非法值：%s=%s（仅允许 allow/ask/deny）" % (k, v), get_policy()
    with _lock:
        policy = get_policy()
        policy.update({k: partial[k] for k in ACTION_KEYS if k in partial})
        _save(_POLICY_FILE, {"policy": policy})
    return True, "", policy


# ---------------------------------------------------------------------------
# 3. 产物沉淀（Artifacts）
# ---------------------------------------------------------------------------
_ARTIFACTS_FILE = "artifacts.json"
ARTIFACTS_MAX = 200
_TITLE_MAX = 200
_KIND_MAX = 32
_CONTENT_MAX = 200000


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def list_artifacts():
    d = _load(_ARTIFACTS_FILE, {"artifacts": []})
    arts = d.get("artifacts")
    if not isinstance(arts, list):
        return []
    return [a for a in arts if isinstance(a, dict) and a.get("id")]


def create_artifact(title, kind, content):
    title = str(title or "").strip()
    kind = str(kind or "text").strip() or "text"
    content = str(content or "")
    if not title:
        return False, "缺少 title", None
    if len(title) > _TITLE_MAX:
        return False, "title 过长（最多 %d 字）" % _TITLE_MAX, None
    if len(kind) > _KIND_MAX:
        return False, "kind 过长", None
    if not content.strip():
        return False, "缺少 content", None
    if len(content) > _CONTENT_MAX:
        return False, "content 过长（最多 %d 字）" % _CONTENT_MAX, None
    art = {
        "id": uuid.uuid4().hex,
        "title": title,
        "kind": kind,
        "content": content,
        "created_at": _now_iso(),
    }
    with _lock:
        arts = list_artifacts()
        arts.insert(0, art)  # 新→旧
        del arts[ARTIFACTS_MAX:]  # 上限 200，淘汰最旧
        _save(_ARTIFACTS_FILE, {"artifacts": arts})
    return True, "", art


def delete_artifact(aid):
    aid = str(aid or "").strip()
    if not aid:
        return False
    with _lock:
        arts = list_artifacts()
        kept = [a for a in arts if a.get("id") != aid]
        if len(kept) == len(arts):
            return False
        _save(_ARTIFACTS_FILE, {"artifacts": kept})
    return True


# ---------------------------------------------------------------------------
# 4. 媒体生成（v1 占位：如实返回，不伪造）
# ---------------------------------------------------------------------------
_MEDIA_KEY_ENV = "QL_MEDIA_API_KEY"
_MEDIA_BASE_ENV = "QL_MEDIA_API_BASE"


def generate_media(prompt):
    """返回 (http_code, payload)。

    v1 没有图片模型管线：无凭证 → 501 media_not_configured；有凭证 →
    501 media_not_implemented（管线预留，绝不返回伪造图片）。
    """
    key = os.environ.get(_MEDIA_KEY_ENV, "").strip()
    if not key:
        return 501, {
            "error": "media_not_configured",
            "hint": "未配置图片模型凭证：自托管请在后端环境变量设置 "
                    "QL_MEDIA_API_KEY（可选 QL_MEDIA_API_BASE）后重试；"
                    "商业版由服务方内置",
        }
    return 501, {
        "error": "media_not_implemented",
        "hint": "媒体生成管线预留中，图片模型尚未接入；凭证已就绪，后续版本启用",
    }


def _generate_with_provider(prompt, key, base=None):
    """未来在此接入图片模型（OpenAI Images 兼容接口等）。v1 永不调用。"""
    raise NotImplementedError("媒体生成管线预留：后续版本在此接入图片模型")


# ---------------------------------------------------------------------------
# 5. 简报口径（v1 占位：只存用户口径，不编造文章）
# ---------------------------------------------------------------------------
_BRIEF_FILE = "brief_config.json"
_BRIEF_MAX = 500


def get_brief():
    d = _load(_BRIEF_FILE, {"brief": ""})
    b = d.get("brief")
    return str(b) if isinstance(b, str) else ""


def set_brief(text):
    text = str(text or "").strip()
    if len(text) > _BRIEF_MAX:
        return False, "简报口径过长（最多 %d 字）" % _BRIEF_MAX
    with _lock:
        _save(_BRIEF_FILE, {"brief": text})
    return True, ""
