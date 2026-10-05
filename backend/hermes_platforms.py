#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes 网关第三方平台（config.yaml 顶层 platforms 段）读写。

App「对接第三方」页的后端：
- GET  /api/hermes/platforms → 每平台 id/name/configured/enabled/needs（token 值永不返回）
- POST /api/hermes/platforms → {platform, enabled, config} 校验必填项后写入
  platforms.<id> 段并重启 gateway

写入为行级改写（保留注释与平台段内其它键），原子替换并沿用原文件权限/owner
（复用 channel_api._write_lines）；重启复用 channel_api._restart_gateway，
不另起一套。

平台静态清单与 needs 取自 Hermes 官方文档（2026-10）；小众平台的键名若与
生产 Hermes 版本有出入，以 gateway 日志为准微调。
只依赖标准库。
"""
import threading

import channel_api

_lock = threading.Lock()

# needs：App 需向用户收集的字段；"qrcode" 表示需扫码/配对，第一版只读状态。
# keys：needs 字段 → config.yaml 实际键名（缺省则字段名即键名）。
PLATFORMS = [
    {"id": "telegram", "name": "Telegram",
     "needs": ["bot_token"], "keys": {"bot_token": "token"}},
    {"id": "discord", "name": "Discord",
     "needs": ["bot_token"], "keys": {"bot_token": "token"}},
    {"id": "slack", "name": "Slack",
     "needs": ["bot_token", "app_token"],
     "keys": {"bot_token": "token", "app_token": "app_token"}},
    {"id": "whatsapp", "name": "WhatsApp",
     "needs": ["qrcode"], "keys": {}},
    {"id": "signal", "name": "Signal",
     "needs": ["qrcode"], "keys": {}},
    {"id": "email", "name": "邮件",
     "needs": ["address", "password", "imap_host", "smtp_host"], "keys": {}},
    {"id": "sms", "name": "短信",
     "needs": ["account_sid", "auth_token", "phone_number", "webhook_url"], "keys": {}},
    {"id": "matrix", "name": "Matrix",
     "needs": ["homeserver", "user_id", "access_token"], "keys": {}},
    {"id": "mattermost", "name": "Mattermost",
     "needs": ["server_url", "token"], "keys": {}},
    {"id": "homeassistant", "name": "Home Assistant",
     "needs": ["base_url", "token"], "keys": {}},
    {"id": "dingtalk", "name": "钉钉",
     "needs": ["webhook_url", "secret"], "keys": {}},
    {"id": "feishu", "name": "飞书",
     "needs": ["app_id", "app_secret"], "keys": {}},
    {"id": "wecom", "name": "企业微信",
     "needs": ["corp_id", "corp_secret"], "keys": {}},
    {"id": "bluebubbles", "name": "BlueBubbles",
     "needs": ["server_url", "password"], "keys": {}},
    {"id": "weixin", "name": "微信",
     "needs": ["qrcode"], "keys": {}},
    {"id": "webhook", "name": "Webhook",
     "needs": ["url"], "keys": {}},
]

_CATALOG = {p["id"]: p for p in PLATFORMS}


def _cfg_key(p, field):
    return p.get("keys", {}).get(field, field)


def _is_qrcode(p):
    return p["needs"] == ["qrcode"]


def _unquote(v):
    """去引号并反转义（与 _yaml_quote 配对）。"""
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        inner = v[1:-1]
        out = []
        i = 0
        while i < len(inner):
            if inner[i] == "\\" and i + 1 < len(inner) and inner[i + 1] in ('"', "\\"):
                out.append(inner[i + 1])
                i += 2
            else:
                out.append(inner[i])
                i += 1
        return "".join(out)
    return v.strip("\"'")


def _read_raw():
    """读 platforms 段 → {platform_id: {key: value}}；文件缺失返回 {}。"""
    try:
        with open(channel_api.PROFILE_CFG, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    blk = channel_api._find_top_block(lines, "platforms:")
    if not blk:
        return {}
    start, end = blk
    out = {}
    cur = None
    for ln in lines[start + 1:end]:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        if indent == 2 and s.endswith(":"):
            cur = s[:-1].strip()
            out[cur] = {}
        elif cur is not None and indent >= 4 and ":" in s:
            k, v = s.split(":", 1)
            out[cur][k.strip()] = _unquote(v)
    return out


def get_platforms():
    """App 展示用清单（不含任何 token 值）。"""
    raw = _read_raw()
    out = []
    for p in PLATFORMS:
        pid = p["id"]
        cfg = raw.get(pid, {})
        enabled = str(cfg.get("enabled", "")).lower() in ("true", "yes", "1")
        if _is_qrcode(p):
            # 扫码/配对类：凭证在网关侧线下完成，enabled 即视为已配置
            configured = enabled
        else:
            configured = all(
                cfg.get(_cfg_key(p, f), "").strip() for f in p["needs"]
            )
        out.append({
            "id": pid,
            "name": p["name"],
            "configured": bool(configured),
            "enabled": enabled,
            "needs": list(p["needs"]),
        })
    return out


def _yaml_quote(v):
    return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')


def _ensure_subsection(lines, pid):
    """确保 platforms 块及 pid 子段存在；返回 (out, sub_start, sub_end)。"""
    blk = channel_api._find_top_block(lines, "platforms:")
    if not blk:
        out = lines + ["", "platforms:", "  %s:" % pid]
        return out, len(out) - 1, len(out)
    pstart, pend = blk
    i = pstart + 1
    while i < pend:
        ln = lines[i]
        s = ln.strip()
        indent = len(ln) - len(ln.lstrip(" "))
        if indent == 2 and s == pid + ":":
            j = i + 1
            while j < pend:
                lj = lines[j]
                sj = lj.strip()
                ij = len(lj) - len(lj.lstrip(" "))
                if sj and not sj.startswith("#") and ij <= 2:
                    break
                j += 1
            return lines, i, j
        i += 1
    out = lines[:pend] + ["  %s:" % pid] + lines[pend:]
    return out, pend, pend + 1


def _write_platform(pid, enabled, kv):
    """写 platforms.<pid> 段：enabled + kv 键；保留段内其它键。"""
    with open(channel_api.PROFILE_CFG, encoding="utf-8") as f:
        lines = f.read().splitlines()
    lines, s, e = _ensure_subsection(lines, pid)
    managed = {"enabled"} | set(kv.keys())
    body = []
    for ln in lines[s + 1:e]:
        t = ln.strip()
        if t and not t.startswith("#") and ":" in t:
            if t.split(":", 1)[0].strip() in managed:
                continue  # 删旧值，稍后统一写
        body.append(ln)
    new_keys = ["    enabled: %s" % ("true" if enabled else "false")]
    for k, v in kv.items():
        new_keys.append("    %s: %s" % (k, _yaml_quote(v)))
    out = lines[:s + 1] + new_keys + body + lines[e:]
    channel_api._write_lines(out)


def set_platform(pid, enabled, config):
    """启用/停用/配置平台。返回 (ok: bool, error_zh: str, restarted: bool)。"""
    p = _CATALOG.get(pid)
    if p is None:
        return False, "不支持的平台：%s" % pid, False
    if not isinstance(enabled, bool):
        return False, "缺少 enabled 参数（true/false）", False
    config = config or {}
    if not isinstance(config, dict):
        return False, "config 须为对象", False
    if _is_qrcode(p):
        if enabled:
            return False, "该平台需要扫码/配对，请先在服务器上完成配置（第一版暂不支持 App 内配置）", False
        # 停用：只关 enabled，保留既有配置
    else:
        if enabled:
            missing = [f for f in p["needs"] if not str(config.get(f, "")).strip()]
            if missing:
                return False, "缺少必填配置：%s" % "、".join(missing), False
    kv = {}
    for f in p["needs"]:
        if f == "qrcode":
            continue
        if f in config and str(config[f]).strip():
            kv[_cfg_key(p, f)] = str(config[f]).strip()
    with _lock:
        try:
            _write_platform(pid, enabled, kv)
        except OSError as e:
            return False, "写入 Hermes 配置失败：%s" % str(e)[:120], False
        except Exception as e:  # noqa: BLE001
            return False, "写入 Hermes 配置失败：%s" % str(e)[:120], False
    restarted = channel_api._restart_gateway()
    return True, "", restarted
