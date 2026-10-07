#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态 feed API（I 线，2026-10-06；2026-10-07 加历史+分页+配图）：供 iOS 资讯 tab。

GET /api/feed/units?prompt=...&limit=6&offset=0&paged=1&refresh=1
  · 默认返回顶层数组（兼容老 iOS）
  · paged=1 返回 {units, offset, has_more, total}
  FeedUnit = {"id","title","bodyMarkdown","category":"tech|ai|oss",
              "imageURL":null,"publishedAt":"ISO8601","likes":0}

链路：Hermes 上游 → 生成 JSON 卡片 → 校验归一化 → 从原文提取 og:image
      → 追加历史（最多 200 条）→ 返回分页。

图片：从 bodyMarkdown 里的真实新闻链接抓 og:image，不编造；抓不到就 null。
"""
import hashlib
import glob
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("QL_DATA_DIR") or os.path.join(os.path.dirname(_BASE_DIR), "data")
CACHE_FILE = os.path.join(DATA_DIR, "feed_cache.json")
HISTORY_FILE = os.path.join(DATA_DIR, "feed_history.json")
# The previous release wrote both files under /tmp. Promote them to QL_DATA_DIR
# the first time the new code reads the feed, before a later container restart
# can discard them.
LEGACY_CACHE_FILE = "/tmp/qingliao_feed_cache.json"
LEGACY_HISTORY_FILE = "/tmp/qingliao_feed_history.json"
HISTORY_MAX = 200  # 最多保留 200 条历史
CACHE_TTL = 6 * 3600          # 动态 6 小时一刷
DEFAULT_PROMPT = "我的兴趣动态版块，围绕三块内容：科技圈的新动态、AI 圈的进展、好玩的开源项目。"
DEFAULT_LIMIT = 6
MAX_LIMIT = 10
LLM_TIMEOUT = 90              # 后台线程跑，不占 iOS 的 8s 超时

_gen_lock = threading.Lock()
_gen_running = False
_history_lock = threading.RLock()

_JSON_ARR_RE = re.compile(r"\[.*\]", re.S)
_CATS = ("tech", "ai", "oss")

_SYSTEM = (
    "你是轻聊 App「动态」版块的内容编辑。根据用户兴趣生成今日动态卡片，"
    "只输出 JSON 数组，不要任何解释、前言或 markdown 代码围栏。"
)

_USER_TMPL = """用户兴趣提示词：{prompt}
今天日期：{today}（UTC）

要求：
1. 生成 {n} 张卡片，category 从 tech（科技圈）/ ai（AI 圈）/ oss（好玩的开源项目）中选，每类至少 1 张。
2. 内容必须是真实、可验证的近期动态（近 7 天内）；标题简洁有力；bodyMarkdown 2-4 句中文，
   可含 1-2 个 markdown 链接 [文字](https://真实URL)，URL 必须是真实存在的（项目官网/新闻原文）。
3. 严禁编造不存在的项目、论文、新闻或 URL；不确定的宁可不写链接。
4. publishedAt 用 ISO8601 UTC（如 2026-10-06T10:00:00Z），分布在过去 48 小时内。
5. 数组元素格式：
   {{"id":"feed-1","title":"标题","bodyMarkdown":"正文，可含[链接](https://…)","category":"tech",
     "imageURL":null,"publishedAt":"2026-10-06T10:00:00Z","likes":0}}
只输出 JSON 数组。"""


def _iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s):
    """尽量解析模型给的时间；失败返回 None（调用方兜底）。"""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _extract_json_array(text):
    """从模型输出里抠 JSON 数组（去围栏 → 取最外层 [...] → loads）。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    m = _JSON_ARR_RE.search(t)
    if not m:
        return []
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _extract_first_url(text):
    """从 markdown 文本提取第一个 http(s) 链接。"""
    if not text:
        return None
    m = re.search(r'https?://[^\s\)\]]+', text)
    return m.group(0) if m else None


def _fetch_og_image(url, timeout=10):
    """抓新闻原文的 og:image。失败返回 None（不编造）。"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; NoriFeed/1.0)",
        }, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 只读前 200KB（og:image 一般在 head 里）
            html = resp.read(204800).decode("utf-8", "replace")
        # 找 og:image
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                      html, re.I)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                          html, re.I)
        if m:
            img = m.group(1).strip()
            if img.startswith(("http://", "https://")):
                return img
            # 相对路径转绝对
            if img.startswith("//"):
                return "https:" + img
            if img.startswith("/"):
                from urllib.parse import urlparse
                p = urlparse(url)
                return "%s://%s%s" % (p.scheme, p.netloc, img)
    except Exception:
        pass
    return None


def _enrich_images(units):
    """为没有配图的资讯从原文提取 og:image（后台线程跑，不阻塞）。"""
    for u in units:
        if not isinstance(u, dict):
            continue
        if u.get("imageURL"):
            continue  # 已有图的不动
        body = u.get("bodyMarkdown") or ""
        url = _extract_first_url(body)
        if not url:
            continue
        img = _fetch_og_image(url)
        if img:
            u["imageURL"] = img
    return units


def _normalize_units(raw):
    """模型原始数组 → iOS FeedUnit 形状；缺字段/坏时间的卡片修掉，修不好就丢（不编造）。"""
    now = datetime.now(timezone.utc)
    out = []
    for i, r in enumerate(raw or []):
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()
        if not title:
            continue  # 无标题的卡不要
        body = str(r.get("bodyMarkdown") or r.get("body") or "").strip()
        cat = str(r.get("category") or "").strip().lower()
        if cat not in _CATS:
            cat = "tech"
        dt = _parse_time(r.get("publishedAt"))
        if dt is None or dt > now + timedelta(minutes=5) or dt < now - timedelta(days=14):
            dt = now - timedelta(hours=2 * i)  # 坏时间 → 按序号倒排兜底
        # 模型常按 prompt 把每批 ID 重置成 feed-1 ... feed-6，不能信任模型 ID。
        # 同一真实链接跨批次保持同一 ID；无链接时按标题+正文指纹区分不同内容。
        uid = _story_id(title, body)
        img = r.get("imageURL")
        if not isinstance(img, str) or not img.startswith(("http://", "https://")):
            img = None
        try:
            likes = int(r.get("likes") or 0)
        except (TypeError, ValueError):
            likes = 0
        out.append({
            "id": uid,
            "title": title,
            "bodyMarkdown": body,
            "category": cat,
            "imageURL": img,
            "publishedAt": _iso_z(dt),
            "likes": likes,
            "_dt": dt,
        })
    # 去重（id 相同只留第一条）→ 按时间倒序
    seen, uniq = set(), []
    for u in out:
        if u["id"] in seen:
            continue
        seen.add(u["id"])
        uniq.append(u)
    uniq.sort(key=lambda u: u["_dt"], reverse=True)
    for u in uniq:
        u.pop("_dt", None)
    return uniq


def _story_id(title, body):
    url = _extract_first_url(body)
    if url:
        try:
            parsed = urllib.parse.urlsplit(url.rstrip(".,，。;；!?！？"))
            host = (parsed.hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            port = ":%d" % parsed.port if parsed.port else ""
            path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
            query = urllib.parse.urlencode(sorted(
                (k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                if not k.lower().startswith("utm_") and k.lower() not in
                {"fbclid", "gclid", "ref", "source"}))
            identity = "url:%s" % urllib.parse.urlunsplit(
                (parsed.scheme.lower() or "https", host + port, path, query, ""))
        except Exception:
            identity = "url:" + url.strip().lower()
    else:
        compact = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
        identity = "text:%s\n%s" % (compact(title), compact(body))
    return "feed-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _read_history():
    """读资讯历史，并把旧 /tmp 历史和旧版六条缓存幂等迁入持久目录。"""
    with _history_lock:
        history = _load_history_file()
        legacy = _read_json_list(LEGACY_HISTORY_FILE)
        for path in glob.glob(os.path.join(DATA_DIR, "feed_legacy_history_*.json")):
            legacy.extend(_read_json_list(path))
        cache = _read_cache()
        legacy_cache_units = []
        for path in glob.glob(os.path.join(DATA_DIR, "feed_legacy_cache_*.json")):
            legacy_cache_units.extend(_read_json_object(path).get("units", []))
        merged = _merge_history(history, legacy, legacy_cache_units,
                                (cache or {}).get("units", []))
        if merged != history:
            _save_history(merged)
        return merged


def _read_json_list(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, list) else []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []


def _read_json_object(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _load_history_file():
    return _read_json_list(HISTORY_FILE)


def _merge_history(*batches):
    by_id = {}
    for batch in batches:
        for unit in batch or []:
            if not isinstance(unit, dict):
                continue
            uid = str(unit.get("id") or "").strip()
            if uid and uid not in by_id:
                by_id[uid] = unit
    def sort_key(unit):
        dt = _parse_time(unit.get("publishedAt"))
        return dt.timestamp() if dt else 0
    return sorted(by_id.values(), key=sort_key, reverse=True)[:HISTORY_MAX]


def _save_history(history):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, HISTORY_FILE)
        return True
    except OSError as exc:
        print("[feed] save persistent history failed: %s" % exc, flush=True)
        return False


def _append_history(new_units):
    """把旧缓存、旧历史和新批次合并后原子落到持久目录，按内容 ID 去重。"""
    with _history_lock:
        cache = _read_cache()
        legacy = _read_json_list(LEGACY_HISTORY_FILE)
        legacy_cache_units = []
        for path in glob.glob(os.path.join(DATA_DIR, "feed_legacy_history_*.json")):
            legacy.extend(_read_json_list(path))
        for path in glob.glob(os.path.join(DATA_DIR, "feed_legacy_cache_*.json")):
            legacy_cache_units.extend(_read_json_object(path).get("units", []))
        hist = _merge_history(_load_history_file(), legacy, legacy_cache_units,
                              (cache or {}).get("units", []), new_units)
        _save_history(hist)


def _read_cache():
    migrated = sorted(glob.glob(os.path.join(DATA_DIR, "feed_legacy_cache_*.json")))
    for path in (CACHE_FILE, *reversed(migrated), LEGACY_CACHE_FILE):
        try:
            with open(path, "r", encoding="utf-8") as f:
                c = json.load(f)
            if isinstance(c, dict) and isinstance(c.get("units"), list):
                if path != CACHE_FILE:
                    _write_cache_file(c)
                return c
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    return None


def _write_cache_file(cache):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError as exc:
        print("[feed] promote legacy cache failed: %s" % exc, flush=True)


def _write_cache(prompt, units):
    try:
        cache = {"prompt": prompt, "units": units, "updated_at": time.time()}
        _write_cache_file(cache)
        _append_history(units)
    except Exception as exc:
        print("[feed] cache/history write failed: %s" % exc, flush=True)


def _generate(prompt, limit):
    """调 Hermes 生成动态卡片。任何失败 → []（上层回诚实空态）。"""
    import hermes_upstream
    base = hermes_upstream.get_base_url()
    if not base:
        return []
    key = hermes_upstream.get_key()
    url = base.rstrip("/") + "/v1/chat/completions"
    # The upstream is Hermes's OpenAI-compatible gateway. The selected provider
    # model lives in Hermes config.yaml; this endpoint accepts the gateway alias.
    model = os.environ.get("QL_FEED_MODEL") or "hermes-agent"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER_TMPL.format(
                prompt=prompt, today=_iso_z(datetime.now(timezone.utc))[:10], n=limit)},
        ],
        "stream": False,
        "temperature": 0.7,
        "model_options": {"reasoning": {"enabled": False}},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
        raw = json.loads(resp.read().decode("utf-8", "replace"))
    content = ((raw.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
    return _normalize_units(_extract_json_array(content))


def _regen_worker(prompt, limit):
    global _gen_running
    try:
        units = _generate(prompt, limit)
        if units:  # 只有成功才覆盖缓存；失败保留旧缓存（不断流）
            # 2026-10-07：从新闻原文提取配图
            _enrich_images(units)
            _write_cache(prompt, units)
    except Exception as exc:
        # Keep provider failures visible in container logs; otherwise the UI's
        # empty state looks like a connection problem even when generation failed.
        print("[feed] generation failed: %s" % str(exc)[:200], flush=True)
    finally:
        with _gen_lock:
            _gen_running = False


def _kick_regen(prompt, limit):
    """缓存过期/缺失时起后台再生。无上游时不做无用功。"""
    global _gen_running
    try:
        import hermes_upstream
        if not hermes_upstream.get_base_url():
            return
    except Exception:
        return
    with _gen_lock:
        if _gen_running:
            return
        _gen_running = True
    t = threading.Thread(target=_regen_worker, args=(prompt, limit),
                         daemon=True, name="ql-feed-gen")
    t.start()


class FeedHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-Auth-Token")

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        import auth_api
        # iOS FeedStore 只发 Authorization: Bearer（与 AuthStore 的 X-Auth-Token 不一致），兼容之
        if not self.headers.get("X-Auth-Token"):
            az = self.headers.get("Authorization", "")
            if az.startswith("Bearer ") and az[7:].strip():
                self.headers["X-Auth-Token"] = az[7:].strip()
        return auth_api.check_auth(self.headers, "X-Feed-Password",
                                   os.environ.get("QL_PASSWORD", ""))

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._check_auth():
            self._send(401, {"error": "未授权"})
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip("/") != "/api/feed/units":
            self._send(404, {"error": "Not Found"})
            return
        qs = urllib.parse.parse_qs(parsed.query)
        prompt = (qs.get("prompt", [""])[0] or "").strip()[:500] or DEFAULT_PROMPT
        try:
            limit = int(qs.get("limit", [DEFAULT_LIMIT])[0])
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = max(1, min(MAX_LIMIT, limit))
        # 2026-10-07：分页参数
        try:
            offset = int(qs.get("offset", ["0"])[0])
        except (TypeError, ValueError):
            offset = 0
        offset = max(0, offset)
        force = (qs.get("refresh", [""])[0] or "").lower() in ("1", "true", "yes")
        # paged=1 时返回 {units, has_more} 对象；默认返回数组（兼容老 iOS）
        want_paged = (qs.get("paged", [""])[0] or "").lower() in ("1", "true", "yes")

        cached = _read_cache()
        same_prompt = bool(cached) and cached.get("prompt") == prompt
        fresh = same_prompt and (time.time() - cached.get("updated_at", 0)) < CACHE_TTL \
            and bool(cached.get("units"))
        # 2026-10-07：从历史取（分页用），没有历史则用缓存
        hist = _read_history()
        all_units = hist if hist else (cached["units"] if cached else [])
        if fresh and not force:
            page = all_units[offset:offset + limit]
            if want_paged:
                self._send(200, {"units": page, "offset": offset,
                                 "has_more": offset + limit < len(all_units),
                                 "total": len(all_units)})
            else:
                self._send(200, page)
            return
        # 过期/缺失/换了提示词 → 后台再生，本次先回旧数据或 []（iOS 8s 超时内必须返回）
        _kick_regen(prompt, limit)
        # Keep the existing feed visible while a changed prompt is regenerated.
        # A prompt change should not turn a healthy service into a misleading
        # "not connected" empty state.
        units = all_units
        page = (units or [])[offset:offset + limit]
        if want_paged:
            self._send(200, {"units": page, "offset": offset,
                             "has_more": offset + limit < len(units or []),
                             "total": len(units or [])})
        else:
            self._send(200, page)

    def log_message(self, fmt, *args):
        pass
