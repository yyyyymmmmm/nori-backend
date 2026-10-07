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
import hermes_inspect


def _restart_gateway():
    """Restart the discovered Hermes container after a live config update."""
    ok, detail = hermes_inspect.restart_hermes_container()
    if not ok:
        print("[hermes] 容器重启失败：%s" % detail, flush=True)
    return ok
