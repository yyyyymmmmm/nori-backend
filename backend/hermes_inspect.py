#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经 docker socket 读 Hermes 容器文件：模型/服务商/记忆/技能的完整同步。

原理：Nori 后端容器挂载了 /var/run/docker.sock，可调 Docker API。
用 docker exec 在 Hermes 容器里 cat 配置文件，拿回完整数据。

Hermes 容器名：环境变量 HERMES_CONTAINER 指定；未指定则自动发现
（镜像名或容器名含 hermes 的第一个运行中容器）。
"""
import json
import os
import socket
import urllib.parse

_DOCKER_SOCK = "/var/run/docker.sock"
_HERMES_CONTAINER = os.environ.get("HERMES_CONTAINER", "")


def _docker_api(method, path, body=None):
    """调 Docker Remote API（经 unix socket）。返回 (status, json_or_text)。"""
    if not os.path.exists(_DOCKER_SOCK):
        return 0, "no docker socket"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(_DOCKER_SOCK)
        headers = {"Host": "localhost", "Content-Type": "application/json",
                   "Connection": "close"}
        payload = json.dumps(body).encode() if body is not None else b""
        if payload:
            headers["Content-Length"] = str(len(payload))
        req_lines = ["%s %s HTTP/1.1" % (method, path)]
        for k, v in headers.items():
            req_lines.append("%s: %s" % (k, v))
        req_lines += ["", ""]
        s.sendall("\r\n".join(req_lines).encode() + payload)
        resp = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            resp += chunk
        s.close()
    except Exception as e:
        return 0, str(e)[:100]

    try:
        header_end = resp.index(b"\r\n\r\n")
        header = resp[:header_end].decode("utf-8", "replace")
        body_bytes = resp[header_end + 4:]
        status = int(header.split(" ", 2)[1])
        # 去 chunked 编码
        if "Transfer-Encoding: chunked" in header:
            out = b""
            i = 0
            while True:
                j = body_bytes.index(b"\r\n", i)
                size = int(body_bytes[i:j].decode().strip(), 16)
                if size == 0:
                    break
                i = j + 2
                out += body_bytes[i:i + size]
                i += size + 2
            body_bytes = out
        text = body_bytes.decode("utf-8", "replace")
        try:
            return status, json.loads(text)
        except Exception:
            return status, text
    except Exception as e:
        return 0, "parse error: %s" % str(e)[:80]


def find_hermes_container():
    """找 Hermes 容器。返回容器 ID 或 None。"""
    if _HERMES_CONTAINER:
        return _HERMES_CONTAINER
    status, data = _docker_api("GET", "/containers/json?all=0")
    if status != 200 or not isinstance(data, list):
        return None
    for c in data:
        names = " ".join(c.get("Names", [])).lower()
        image = str(c.get("Image", "")).lower()
        if "hermes" in names or "hermes" in image:
            return c.get("Id")
    return None


def exec_in_hermes(cmd):
    """在 Hermes 容器里执行命令。返回 (ok, stdout)。"""
    cid = find_hermes_container()
    if not cid:
        return False, "hermes container not found"
    # 创建 exec
    status, data = _docker_api("POST", "/containers/%s/exec" % cid, {
        "AttachStdout": True, "AttachStderr": True,
        "Cmd": ["sh", "-c", cmd],
    })
    if status not in (200, 201) or not isinstance(data, dict):
        return False, "exec create failed: %s" % str(data)[:100]
    exec_id = data.get("Id")
    if not exec_id:
        return False, "no exec id"
    # 启动 exec（demuxed stream，简化处理取 stdout）
    status, out = _docker_api("POST", "/exec/%s/start" % exec_id,
                              {"Detach": False, "Tty": False})
    if status != 200:
        return False, "exec start failed"
    # Docker exec 输出是 multiplexed stream：每帧 8 字节头 + payload
    # 简化：去掉帧头拼起来
    if isinstance(out, str):
        raw = out.encode("utf-8", "replace")
    else:
        raw = str(out).encode("utf-8", "replace")
    text = b""
    i = 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        text += raw[i + 8:i + 8 + size]
        i += 8 + size
        if size == 0:
            break
    return True, text.decode("utf-8", "replace")


def read_hermes_file(path):
    """读 Hermes 容器里的文件。返回 (ok, content)。"""
    # 防路径穿越
    if ".." in path or not path.startswith("/"):
        return False, "bad path"
    ok, out = exec_in_hermes("cat '%s' 2>/dev/null" % path.replace("'", "'\\''"))
    if not ok:
        return False, out
    if not out.strip():
        return False, "empty or not found"
    return True, out


def list_hermes_dir(path):
    """列 Hermes 容器里的目录。返回 (ok, [names])。"""
    ok, out = exec_in_hermes("ls -1 '%s' 2>/dev/null" % path.replace("'", "'\\''"))
    if not ok:
        return False, []
    names = [l.strip() for l in out.splitlines() if l.strip()]
    return True, names


def probe():
    """探测：容器在不在、关键文件在不在。返回诊断 dict。"""
    result = {"container": None, "files": {}}
    cid = find_hermes_container()
    result["container"] = (cid[:12] if cid else None)
    if not cid:
        return result
    # 2026-10-07 用户实测：NAS 上 Hermes 文件在 /home/agent/.hermes/ 下
    for label, path in [
        ("MEMORY.md", "/home/agent/.hermes/memories/MEMORY.md"),
        ("USER.md", "/home/agent/.hermes/memories/USER.md"),
        ("config", "/home/agent/.hermes/config.yaml"),
        ("state.db", "/home/agent/.hermes/state.db"),
    ]:
        ok, _ = read_hermes_file(path)
        result["files"][label] = path if ok else False
        if not ok:
            # 兜底试别的常见路径
            for alt in ["/app/memories/MEMORY.md", "/app/memories/USER.md",
                        "/app/config.yaml", "/app/state.db",
                        "/data/memories/MEMORY.md", "/root/.hermes/memories/MEMORY.md"]:
                if label.lower().replace(".", "") in alt.lower() or \
                   (label == "config" and "config" in alt):
                    ok2, _ = read_hermes_file(alt)
                    if ok2:
                        result["files"][label] = alt
                        break
    return result


def get_hermes_config():
    """读 Hermes 的 config.yaml。返回 (ok, dict)。"""
    for path in ["/home/agent/.hermes/config.yaml",
                 "/home/agent/.hermes/config.yml",
                 "/app/config.yaml"]:
        ok, content = read_hermes_file(path)
        if ok:
            try:
                import yaml
                return True, yaml.safe_load(content)
            except ImportError:
                # 无 yaml 库，简单解析 providers 段
                return True, {"_raw": content, "_path": path}
            except Exception as e:
                return False, str(e)[:100]
    return False, "config not found"


def get_hermes_models():
    """从 Hermes 配置提取模型列表。返回 [{id, name, provider}]。
    2026-10-07 用户实测：自定义服务商在顶层 custom_providers，当前模型 gpt-5.6-terra，
    服务商 custom:老狗。"""
    ok, cfg = get_hermes_config()
    if not ok:
        return False, []
    models = []
    if isinstance(cfg, dict):
        # 1. custom_providers：自定义服务商（用户实测结构）
        cp = cfg.get("custom_providers")
        if isinstance(cp, dict):
            for pname, pinfo in cp.items():
                if isinstance(pinfo, dict):
                    pmodels = pinfo.get("models") or pinfo.get("model_list") or []
                    if isinstance(pmodels, dict):
                        pmodels = list(pmodels.keys())
                    if isinstance(pmodels, list):
                        for m in pmodels:
                            mid = m if isinstance(m, str) else str(m.get("id", m))
                            models.append({
                                "id": str(mid),
                                "name": str(mid),
                                "provider": str(pname),
                            })
                    # 服务商本身也可能直接列模型
                    if not pmodels and pinfo.get("model"):
                        models.append({
                            "id": str(pinfo["model"]),
                            "name": str(pinfo["model"]),
                            "provider": str(pname),
                        })
                elif isinstance(pinfo, list):
                    for m in pinfo:
                        models.append({"id": str(m), "name": str(m), "provider": str(pname)})
        # 2. models / providers（标准结构）
        for key in ("models", "providers"):
            section = cfg.get(key)
            if isinstance(section, dict):
                for mid, info in section.items():
                    if isinstance(info, dict):
                        models.append({
                            "id": str(mid),
                            "name": str(info.get("name") or info.get("label") or mid),
                            "provider": str(info.get("provider") or "hermes"),
                        })
                    elif isinstance(info, str):
                        models.append({"id": str(mid), "name": info, "provider": "hermes"})
            elif isinstance(section, list):
                for item in section:
                    if isinstance(item, dict) and item.get("id"):
                        models.append({
                            "id": str(item["id"]),
                            "name": str(item.get("name") or item["id"]),
                            "provider": str(item.get("provider") or "hermes"),
                        })
                    elif isinstance(item, str):
                        models.append({"id": item, "name": item, "provider": "hermes"})
        # 3. 当前选中的模型（default_model / model / current_model）
        current = cfg.get("default_model") or cfg.get("model") or cfg.get("current_model")
        if current and isinstance(current, str):
            if not any(m["id"] == current for m in models):
                # 从 custom_providers 找它属于哪个服务商
                prov = "hermes"
                if isinstance(cp, dict):
                    for pname in cp:
                        if current.startswith(str(pname).split(":")[-1]) or pname in current:
                            prov = str(pname)
                            break
                models.append({"id": current, "name": current, "provider": prov,
                               "selected": True})
    return True, models


def get_hermes_memory():
    """读 Hermes 的记忆文件。返回 {MEMORY.md: content, USER.md: content}。"""
    mem = {}
    for label, path in [("MEMORY.md", "/home/agent/.hermes/memories/MEMORY.md"),
                        ("USER.md", "/home/agent/.hermes/memories/USER.md")]:
        ok, content = read_hermes_file(path)
        if ok:
            mem[label] = content
    return mem
