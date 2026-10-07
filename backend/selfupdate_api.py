#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后端自更新 API：/api/selfupdate

让 App 内一键更新后端，用户不用开 SSH 敲 update.sh。

POST /api/selfupdate  {"action": "check"}          → 同步检查远端有没有新版本（秒回）
POST /api/selfupdate  {"action": "run"}            → 后台启动更新，返回 {"task": "..."}
GET  /api/selfupdate?task=<id>                     → 轮询更新进度/结果
GET  /api/selfupdate                               → 当前状态（idle/running/done/failed + 上次结果）

设计要点：
1. **鉴权**：全部走 auth_api.check_auth（X-Auth-Token）。这是能改线上代码的接口，绝不能免鉴权。
2. **执行形态（重要）**：后端跑在容器里（代码 COPY 进镜像，无 git），更新必须动**宿主**的
   git 仓 + docker compose。做法：经挂载的 docker.sock 起一个一次性 helper 容器
   （官方 docker:cli 镜像，自带 git + docker + compose 插件），挂载宿主仓根，在里面跑
   update.sh。update.sh 内部的 `docker compose up -d --build` 经同一个 sock 操作宿主
   daemon —— 等价于用户在宿主敲 ./update.sh。
   仓根路径由 QL_REPO_DIR 环境变量提供（install.sh 安装时自动写入 .env）。
3. **更新中自断**：helper 重建 qingliao 容器那一刻，本 API 所在进程随之消失 →
   更新任务的进度文件落盘（QL_DATA_DIR/selfupdate_state.json），容器回来后轮询接口
   从文件恢复状态；App 侧轮询 502/超时视为「正在重启」，继续等即可。
4. **防并发**：同一时刻只允许一个更新任务（状态文件里 running 标记 + 开始时间；
   超过 15 分钟的 running 视为僵尸，允许重新发起）。
5. **非 git 部署**（手动拷代码跑，无 QL_REPO_DIR）：check 阶段不做任何破坏性动作，
   且**返回 ok:true / update_available:false**（不是 ok:false）—— 见 _manual_deploy_result：
   App 侧把 ok:false 一律渲染成红字「无法自动更新」，对版本已最新的手动实例是假警报。
   run（一键更新）仍然明确报错，不假装成功。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler

try:
    import auth_api
except Exception:          # 与其他模块一致：本机自测无 auth_api 时退化为「无 QL_PASSWORD 放行」
    auth_api = None

REPO_DIR = os.environ.get("QL_REPO_DIR", "")
DATA_DIR = os.environ.get("QL_DATA_DIR", "/data")
STATE_PATH = os.path.join(DATA_DIR, "selfupdate_state.json")

# helper 镜像：官方 docker cli，自带 git + docker + compose 插件
HELPER_IMAGE = os.environ.get("QL_SELFUPDATE_HELPER_IMAGE", "docker:cli")
ZOMBIE_SECONDS = 15 * 60      # running 超过 15 分钟视为僵尸
LOG_TAIL_LINES = 60           # 给 App 回显的日志尾部行数


# ────────────────────────── 状态落盘 ──────────────────────────

def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"status": "idle"}
    except Exception:
        return {"status": "idle"}


def _save_state(state):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _tail_log(state, n=LOG_TAIL_LINES):
    log_path = state.get("log_path", "")
    if not log_path:
        return ""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def _normalize(state):
    """给 App 的视图：状态 + 当前版本 + 日志尾部。running 僵尸自动纠正。"""
    s = dict(state)
    # v4.4.x 加固：helper 写的结果文件（backed_up/health_check/rolled_back）
    task = s.get("task", "")
    if task:
        rp = os.path.join(DATA_DIR, f"selfupdate_{task}.result.json")
        try:
            with open(rp, encoding="utf-8") as f:
                r = json.load(f)
            if isinstance(r, dict):
                s["backed_up"] = bool(r.get("backed_up"))
                s["health_check"] = bool(r.get("health_check"))
                s["rolled_back"] = bool(r.get("rolled_back"))
                s["backup_tag"] = str(r.get("backup_tag") or "")
                # 有结果文件 → 流程已结束，按健康检查定最终状态
                if s.get("status") == "running":
                    if r.get("health_check"):
                        s["status"] = "done"
                    elif r.get("rolled_back"):
                        s["status"] = "failed"
                        s["error"] = "更新后健康检查未通过，已自动回滚到 %s" % (
                            r.get("backup_tag") or "备份")
                    else:
                        s["status"] = "failed"
                        s["error"] = "更新失败（详见日志）"
                    _save_state({k: v for k, v in s.items() if k != "log_tail"})
        except Exception:
            pass
    if s.get("status") == "running":
        started = float(s.get("started_at") or 0)
        if started and time.time() - started > ZOMBIE_SECONDS:
            s["status"] = "failed"
            s["error"] = "更新超时（15 分钟未完成），请到 NAS 上手动执行 ./update.sh 排查"
            _save_state({k: v for k, v in s.items() if k != "log_tail"})
    s["log_tail"] = _tail_log(s)
    # 回传本机当前版本（App 比对用）
    try:
        import version_api
        v = version_api.get_version_info()
        s["current"] = {"version": v.get("version", ""), "commit": v.get("commit", "")}
    except Exception:
        s["current"] = {}
    s.pop("log_path", None)          # 内部路径不外泄
    return s


# ────────────────────────── 环境探测 ──────────────────────────

def _env_check():
    """返回 (ok, error)。容器内探测 sock / 宿主仓根是否可用。"""
    if not REPO_DIR:
        return False, ("未配置 QL_REPO_DIR（宿主 git 仓路径）。"
                       "请更新 install.sh 重新安装，或在 .env 里补 "
                       "QL_REPO_DIR=宿主上 qingliao-backend 仓的绝对路径 后重启容器")
    if not shutil.which("docker"):
        return False, "容器内没有 docker CLI（镜像缺 /usr/bin/docker 挂载），无法发起宿主更新"
    if not os.path.exists("/var/run/docker.sock"):
        return False, "容器未挂载 /var/run/docker.sock，无法操作宿主 Docker"
    return True, ""


def _host_repo_exists():
    """helper 视角校验宿主仓根：直接看宿主根挂载点下有没有 update.sh。
    容器内看不到宿主原路径，改由 helper 容器内判断（-v REPO_DIR:/repo 后 ls /repo/update.sh）。"""
    return True   # 真正的校验在 helper 启动命令里做（失败会写进日志）


# ────────────────────────── check（同步） ──────────────────────────

def _manual_deploy_result(detail=""):
    """非 git 装法（bind mount / 手动部署）的 check 结论 —— ok:True 而不是报错。

    这类部署没有宿主 git 仓可查、`update.sh` 也跑不了：旧逻辑回 ok:false，
    而 App 侧（BackendUpdate.swift preciseCheck）把 ok:false 一律渲染成红字
    「无法自动更新」，对"版本本来就是最新"的实例是假警报（实测：手动装法 +
    /api/version 已 v4.0.22，设置页仍挂着红字「未配置 QL_REPO_DIR」）。

    改回 ok:true / update_available:false 的含义是「没有可自动执行的更新」，
    并在 log 里如实说明"查不了远端、请按本机装法手动更新"——不假装比对过。
    run（一键更新）路径不动：真去点仍然会明确报错，不会静默假装成功。
    """
    try:
        import version_api
        info = version_api.get_version_info()
        cur = str(info.get("version") or "").strip()
        commit = str(info.get("commit") or "").strip()
    except Exception:
        cur, commit = "", ""
    lines = []
    if detail:
        lines.append(detail.strip())
    lines += [
        "本部署是手动装法（未配置 QL_REPO_DIR），没有宿主 git 仓可查，"
        "无法自动核对远端是否有新提交。",
        (f"当前后端版本：{cur}" if cur else
         (f"当前构建标识：{commit}（没有版本号）" if commit else
          "当前后端没有可识别的版本号或 Git 提交信息")),
        "要更新请按本机原有方式手动执行（本机不支持 App 一键更新）。",
    ]
    return {"ok": True, "update_available": False, "behind": 0,
            "manual": True, "version_known": bool(cur),
            "current_version": cur, "commit": commit,
            "log": "\n".join(lines)}


def _run_check():
    ok, err = _env_check()
    if not ok:
        if not REPO_DIR:
            # 非 git 装法：不是故障，别让 App 报红（详见 _manual_deploy_result）
            # 不带原始 env 报错文本：那句"请重装 install.sh"对手动装法的用户是误导
            return _manual_deploy_result()
        # 其余（docker CLI / docker.sock 缺失）是真故障，照旧报错
        return {"ok": False, "error": err}
    try:
        r = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{REPO_DIR}:/repo",
             "-w", "/repo", HELPER_IMAGE,
             "sh", "-c", "./update.sh --check 2>&1 || sh ./update.sh --check 2>&1"],
            capture_output=True, text=True, timeout=90)
        out = (r.stdout or "") + (r.stderr or "")
        # update.sh --check 的三种典型输出：
        #   「已是最新」 / 「待更新 N 个提交」 / 「当前目录不是 git 仓库」等致命错
        behind = re.search(r"待更新\s*(\d+)\s*个提交", out)
        if behind:
            return {"ok": True, "update_available": True,
                    "behind": int(behind.group(1)), "log": out[-1500:]}
        if "已是最新" in out:
            return {"ok": True, "update_available": False, "behind": 0, "log": out[-800:]}
        if "不是 git 仓库" in out or "缺少 .env" in out:
            return {"ok": False, "error": "非标准部署（找不到 git 仓或 .env），请在 NAS 上手动更新",
                    "log": out[-800:]}
        return {"ok": False, "error": "检查失败（详见 log）", "log": out[-1500:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "检查超时（90 秒），NAS 可能连不上 GitHub"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


# ────────────────────────── run（异步） ──────────────────────────

def _do_update(task_id):
    state = _load_state()
    state.update({"status": "running", "task": task_id,
                  "started_at": time.time(), "error": "", "log_path": "",
                  "backed_up": False, "health_check": False,
                  "rolled_back": False, "backup_tag": ""})
    _save_state(state)

    log_path = os.path.join(DATA_DIR, f"selfupdate_{task_id}.log")
    result_path = os.path.join(DATA_DIR, f"selfupdate_{task_id}.result.json")
    state["log_path"] = log_path
    _save_state(state)

    def _w(msg):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    try:
        ok, err = _env_check()
        if not ok:
            state.update({"status": "failed", "error": err})
            _save_state(state)
            _w("ENV-CHECK FAILED: " + err)
            return

        _w(f"[{time.strftime('%F %T')}] 启动 helper 更新（镜像 {HELPER_IMAGE}）")
        # v4.4.x 加固：helper 内编排 备份→更新→健康检查→失败回滚 全流程，
        # 结果写 result json（挂载 DATA_DIR），App 轮询拿 backed_up/health_check/rolled_back。
        # 更新会重建 qingliao 容器 → 本进程被杀 → 状态已落盘，回来后靠状态文件+结果文件汇报。
        helper_script = r"""
set -u
REPO=/repo
DATADIR=/su-data
TS=$(date +%Y%m%d-%H%M%S)
TAG="backup-$TS"
RESULT="$DATADIR/SELFUPDATE_TASK.result.json"
LOG="$DATADIR/SELFUPDATE_TASK.log"
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
jbool() { [ "$1" = true ] && echo true || echo false; }

BACKED_UP=false; UPDATE_OK=false; HEALTH_OK=false; ROLLED_BACK=false

cd "$REPO" || { log "FATAL: 仓根不可用"; exit 1; }

# 1) 备份：打 tag（只记代码位置，不碰 data/）
log "步骤1/4：备份代码（git tag $TAG）…"
if git tag "$TAG" 2>>"$LOG"; then
  BACKED_UP=true; log "备份完成：$TAG"
else
  log "备份失败（继续更新，失败时无法自动回滚）"
fi

# 2) 更新
log "步骤2/4：执行 ./update.sh …"
if ./update.sh >>"$LOG" 2>&1; then
  UPDATE_OK=true; log "update.sh 执行完成"
else
  log "update.sh 执行失败"
fi

# 3) 健康检查：等容器回来，轮询 /api/version（9123 为统一入口）
log "步骤3/4：健康检查（轮询 /api/version，最长 6 分钟）…"
if [ "$UPDATE_OK" = true ]; then
  for i in $(seq 1 36); do
    sleep 10
    if curl -sf --max-time 5 http://127.0.0.1:9123/api/version >>"$LOG" 2>&1; then
      HEALTH_OK=true; log "健康检查通过"; break
    fi
  done
  [ "$HEALTH_OK" = true ] || log "健康检查失败（6 分钟未恢复）"
fi

# 4) 失败回滚
if [ "$HEALTH_OK" != true ] && [ "$BACKED_UP" = true ]; then
  log "步骤4/4：回滚到 $TAG …"
  if git checkout -q "$TAG" 2>>"$LOG" && docker compose up -d --build >>"$LOG" 2>&1; then
    ROLLED_BACK=true; log "已回滚，等待恢复…"
    for i in $(seq 1 18); do
      sleep 10
      curl -sf --max-time 5 http://127.0.0.1:9123/api/version >>"$LOG" 2>&1 && break
    done
  else
    log "回滚失败！请手动处理：git checkout $TAG && docker compose up -d --build"
  fi
fi

# 写结果（App 轮询用）
cat > "$RESULT" <<EOF
{"backed_up": $(jbool $BACKED_UP), "health_check": $(jbool $HEALTH_OK), "rolled_back": $(jbool $ROLLED_BACK), "backup_tag": "$TAG", "update_ok": $(jbool $UPDATE_OK)}
EOF
log "流程结束：backed_up=$BACKED_UP health_check=$HEALTH_OK rolled_back=$ROLLED_BACK"
""".replace("SELFUPDATE_TASK", task_id)
        subprocess.Popen(
            ["docker", "run", "--rm",
             "-v", f"{REPO_DIR}:/repo",
             "-v", f"{DATA_DIR}:/su-data",
             "-v", "/var/run/docker.sock:/var/run/docker.sock",
             "-w", "/repo", HELPER_IMAGE,
             "sh", "-c", helper_script],
            stdout=open(log_path, "a", encoding="utf-8"),
            stderr=subprocess.STDOUT)
        # 不 wait：本进程随时会随容器重建被杀。若 helper 失败而本容器还活着
        # （比如 rebuild 没发生），由僵尸超时机制兜底纠正状态。
    except Exception as e:
        state.update({"status": "failed", "error": str(e)[:150]})
        _save_state(state)


_tasks_lock = threading.Lock()


def _start_update():
    state = _load_state()
    if state.get("status") == "running" and \
            time.time() - float(state.get("started_at") or 0) < ZOMBIE_SECONDS:
        return {"ok": False, "error": "已有更新在进行中", "state": _normalize(state)}
    with _tasks_lock:
        task_id = uuid.uuid4().hex[:12]
        threading.Thread(target=_do_update, args=(task_id,), daemon=True).start()
        return {"ok": True, "task": task_id,
                "hint": "更新会重启后端（约 1-3 分钟），期间 App 会短暂失联，请稍候轮询"}


# ────────────────────────── Handler ──────────────────────────

class Handler(BaseHTTPRequestHandler):
    """POST /api/selfupdate  {"action": "check"|"run"}
       GET  /api/selfupdate[?task=...]"""

    def _auth(self):
        if auth_api is not None:
            return auth_api.check_auth(self.headers, "X-Life-Password",
                                       os.environ.get("QL_PASSWORD", ""))
        return not os.environ.get("QL_PASSWORD")

    def _send(self, code, obj):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-Auth-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self._cors()
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        if not self._auth():
            self._send(401, {"ok": False, "error": "未授权"})
            return
        self._send(200, _normalize(_load_state()))

    def do_POST(self):
        if not self._auth():
            self._send(401, {"ok": False, "error": "未授权"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            req = {}
        action = (req.get("action") or "").strip()
        if action == "check":
            self._send(200, _run_check())
        elif action == "run":
            self._send(200, _start_update())
        else:
            self._send(400, {"ok": False, "error": "action 须为 check 或 run"})

    def log_message(self, format, *args):
        pass
