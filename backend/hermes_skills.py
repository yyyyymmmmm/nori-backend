#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes Agent 技能（skills）清单与开关。

Hermes 官方的技能开关机制（`hermes skills config` 交互式 CLI；依据
nousresearch/hermes-agent 官方 FAQ website/docs/reference/faq.md
「Managing skills on Telegram」一节与 #642 号 PR 的 config schema）：
  1. 技能 = 含 SKILL.md 清单的目录，装在 skills 目录里。
     本部署：宿主 QL_HOST_SKILLS_DIR → hermes 容器内 /opt/data/skills
     （与 clouddrive_api.HOST_SKILLS_DIR 同源）。
  2. 开关状态写在主 config.yaml 的 skills 段，与官方 CLI 写入格式一致：
       skills:
         disabled: []                  # 全局禁用 ← 本接口读写这个
         platform_disabled:
           telegram: [skill-a]         # 按平台禁用（v1 不碰，原样保留）
  3. 改完必须重启 gateway（`hermes gateway restart`）才生效——官方 FAQ 原话；
     重启复用 channel_api._restart_gateway，不另起一套。

App「智能体 → 技能」页的后端：
- GET  /api/hermes/skills → 每技能 id/name/description/enabled
- POST /api/hermes/skills → {skill_id, enabled} 改 disabled 列表并重启 gateway
只依赖标准库。
"""
import os
import re
import threading

import channel_api

_lock = threading.Lock()

# 宿主 skills 目录（与 clouddrive_api.HOST_SKILLS_DIR 同源；qingliao 容器挂载 /volume1 可见）
SKILLS_DIR = os.environ.get("QL_HOST_SKILLS_DIR", "/data/hermes/skills")


def _parse_skill_meta(skill_dir):
    """读 SKILL.md 的 YAML frontmatter 取 name/description；没有则回退目录名。"""
    name, desc = None, ""
    try:
        with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as f:
            text = f.read(4096)
    except OSError:
        return None, ""
    m = re.match(r"\s*---\s*\n(.*?)\n\s*---\s*", text, re.S)
    if m:
        for ln in m.group(1).splitlines():
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            k = k.strip().lower()
            v = v.strip().strip("\"'")
            if k == "name" and v:
                name = v
            elif k == "description" and v and not desc:
                desc = v
    return name, desc


def _installed_skills():
    """扫描 skills 目录 → [(skill_id, name, description)]；目录缺失返回 []。"""
    out = []
    try:
        entries = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return []
    for e in entries:
        d = os.path.join(SKILLS_DIR, e)
        if not os.path.isdir(d):
            continue
        if not os.path.isfile(os.path.join(d, "SKILL.md")):
            continue
        name, desc = _parse_skill_meta(d)
        out.append((e, name or e, desc))
    return out


def _read_disabled():
    """读 config.yaml skills.disabled → set(skill_id)；文件/段缺失返回空集。"""
    try:
        with open(channel_api.PROFILE_CFG, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return set()
    blk = channel_api._find_top_block(lines, "skills:")
    if not blk:
        return set()
    start, end = blk
    disabled = set()
    i = start + 1
    while i < end:
        ln = lines[i]
        s = ln.strip()
        indent = len(ln) - len(ln.lstrip(" "))
        if indent == 2 and s.startswith("disabled:"):
            rest = s[len("disabled:"):].strip()
            # 行内式：disabled: [] 或 disabled: [a, b]
            if rest.startswith("["):
                inner = rest[1:rest.find("]")] if "]" in rest else rest[1:]
                for item in inner.split(","):
                    item = item.strip().strip("\"'")
                    if item:
                        disabled.add(item)
                return disabled
            # 块式：后续 4 缩进的 - item 行
            j = i + 1
            while j < end:
                lj = lines[j]
                sj = lj.strip()
                ij = len(lj) - len(lj.lstrip(" "))
                if not sj or sj.startswith("#"):
                    j += 1
                    continue
                if ij <= 2:
                    break
                if sj.startswith("-"):
                    item = sj[1:].strip().strip("\"'")
                    if item:
                        disabled.add(item)
                j += 1
            return disabled
        i += 1
    return disabled


def _write_disabled(disabled):
    """重写 skills.disabled 列表（行级改写，保留注释与 platform_disabled 等其它键）。"""
    with open(channel_api.PROFILE_CFG, encoding="utf-8") as f:
        lines = f.read().splitlines()
    blk = channel_api._find_top_block(lines, "skills:")
    if not blk:
        # 无 skills 段：末尾追加
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("skills:")
        s, e = len(lines) - 1, len(lines)
    else:
        s, e = blk
    # 删掉旧的 disabled 行（含其列表项），其它行原样保留
    body = []
    i = s + 1
    while i < e:
        ln = lines[i]
        t = ln.strip()
        indent = len(ln) - len(ln.lstrip(" "))
        if indent == 2 and t.startswith("disabled:"):
            i += 1
            # 跳过块式列表项
            while i < e:
                lj = lines[i]
                sj = lj.strip()
                ij = len(lj) - len(lj.lstrip(" "))
                if not sj or sj.startswith("#"):
                    # 空行/注释：只跳过紧跟的列表项，注释保留较复杂——简单起见，
                    # disabled 段内的纯注释行一并丢弃（官方 CLI 也是整段重写）
                    if sj.startswith("#") and ij > 2:
                        i += 1
                        continue
                    break
                if ij <= 2:
                    break
                i += 1
            continue
        body.append(ln)
        i += 1
    new_block = ["  disabled:"]
    for sid in sorted(disabled):
        # skill_id 只允许安全字符（调用方已校验），这里再做一层转义
        new_block.append('    - "%s"' % sid.replace("\\", "\\\\").replace('"', '\\"'))
    out = lines[:s + 1] + new_block + body + lines[e:]
    channel_api._write_lines(out)


def list_skills():
    """App 展示用：每技能 id/name/description/enabled（enabled=不在 disabled 列表）。"""
    disabled = _read_disabled()
    return [
        {"id": sid, "name": name, "description": desc, "enabled": sid not in disabled}
        for sid, name, desc in _installed_skills()
    ]


def set_skill(skill_id, enabled):
    """开关技能。返回 (ok: bool, error_zh: str, restarted: bool)。"""
    sid = str(skill_id or "").strip()
    if not sid or not re.fullmatch(r"[A-Za-z0-9_.-]+", sid):
        return False, "无效的技能 ID", False
    if not isinstance(enabled, bool):
        return False, "缺少 enabled 参数（true/false）", False
    with _lock:
        installed = {s[0] for s in _installed_skills()}
        if sid not in installed:
            return False, "技能不存在或未安装：%s" % sid, False
        disabled = _read_disabled()
        if enabled:
            disabled.discard(sid)
        else:
            disabled.add(sid)
        try:
            _write_disabled(disabled)
        except OSError as exc:
            return False, "写入 Hermes 配置失败：%s" % exc, False
        restarted = channel_api._restart_gateway()
        return True, "", restarted
