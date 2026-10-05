#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes config.yaml 写入与 gateway 重启的共享底层。

2026-10-06 迁移：旧的 /api/channel/* 接口（微信通道独立模型 /api/channel/model、
视觉模型 /api/channel/vision-model）已下掉——
- 微信通道的启用/停用/配置 → POST /api/hermes/platforms（平台 weixin）
- 模型选择 → POST /api/hermes/model（全局统一，不再有独立通道模型）
- 视觉模型 → 不再单独配置（主模型原生 vision）
本模块仅保留 config.yaml 原子写入（保留权限/owner）与 gateway 重启，
供 hermes_platforms.py 与 stream_api.py 复用。
"""
import os
import subprocess
import tempfile
import threading

# Hermes 网关真正读取的主 config.yaml（与 mcp_api.HERMES_CONFIG_PATH 同源）
_DEFAULT_CFG = os.environ.get("QL_HERMES_CONFIG", "/volume1/docker/hermes/hermes-data/config.yaml")
_FALLBACK_CFG = "/volume1/docker/hermes/hermes-data/hermes_config.yaml"
PROFILE_CFG = os.environ.get("QL_WECHAT_PROFILE_CFG") or (
    _DEFAULT_CFG if os.path.exists(_DEFAULT_CFG) else _FALLBACK_CFG
)

_lock = threading.Lock()


def _write_lines(out):
    """原子替换主 config.yaml（原来 3 处 `open(PROFILE_CFG,"w")` 是截断写——
    写到一半进程被杀/容器重启就留下半截 YAML，而函数照样 return True，坏文件要到
    Hermes 下次启动才暴露）。权限沿用原文件：Hermes 侧要能读，这里不擅自改。"""
    text = "\n".join(out) + "\n"
    try:
        _st = os.stat(PROFILE_CFG)
        _mode = _st.st_mode & 0o777
        _uid, _gid = _st.st_uid, _st.st_gid
    except OSError:
        _mode = 0o644
        _uid = _gid = None
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PROFILE_CFG) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, _mode)
        os.replace(tmp, PROFILE_CFG)
        # 2026-09-21 修：owner 复原（mkstemp 产物属 root → Hermes 侧读不了 config.yaml）
        if _uid is not None:
            try:
                os.chown(PROFILE_CFG, _uid, _gid)
            except OSError:
                pass
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass
        raise


def _find_top_block(lines, header):
    """找顶层 ``header:`` 块，返回 [start, end) 行索引（含 header 行）；找不到返回 None。
    块结束 = 下一个无缩进的非注释非空行。"""
    for i, ln in enumerate(lines):
        if ln.strip() == header and not ln[0] in " \t":
            end = i + 1
            while end < len(lines):
                nxt = lines[end]
                if nxt.strip() and not nxt[0] in " \t" and not nxt.strip().startswith("#"):
                    break
                end += 1
            return i, end
    return None


def _restart_gateway():
    """重启 Hermes gateway 使新配置生效（docker exec 容器内，异步不阻塞）"""
    try:
        subprocess.Popen(
            ["docker", "exec", "hermes-hermes-1", "hermes", "gateway", "restart"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False
