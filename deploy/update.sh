#!/usr/bin/env bash
# =============================================================================
# 智绘电路 · 服务器端一键更新脚本
# =============================================================================
# 用途：本地重新打包上传后，一条命令完成「停旧服务 → 覆盖代码 → 重启 → 自检」
#
# 用法（在服务器上）：
#     bash /srv/zhihui/deploy/update.sh                       # 默认包 /root/zhihui.tar.gz
#     bash /srv/zhihui/deploy/update.sh /path/to/new.tar.gz   # 指定包路径
#     bash /srv/zhihui/deploy/update.sh --no-verify           # 跳过回归测试（更快）
#
# 设计原则：只操作自己的目录与自己的进程，绝不触碰服务器上其他项目。
# =============================================================================

set -euo pipefail

# ---- 可配置项（按需修改；也可用环境变量覆盖，便于测试）--------------------
APP_DIR="${APP_DIR:-/srv/zhihui}"     # 部署目录（本脚本所在项目的根）
PORT="${PORT:-18080}"                 # 监听端口
PYTHON="${PYTHON:-python3.11}"        # Python 解释器（须 >= 3.10）
PKG="${1:-/root/zhihui.tar.gz}"       # 默认从上传目录取包
VERIFY=1                              # 是否跑回归测试
# ---------------------------------------------------------------------------

# 解析 --no-verify 参数（放在任何位置都能识别）
for arg in "$@"; do
    [ "$arg" = "--no-verify" ] && VERIFY=0
done
# 若第一个参数是包路径则采用；是 -- 开头的选项则忽略
case "$PKG" in --*) PKG="/root/zhihui.tar.gz" ;; esac

PID_FILE="$APP_DIR/server.pid"
LOG_FILE="$APP_DIR/server.log"

say()  { printf '\033[1;36m[更新]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[成功]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[注意]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 0. 前置检查 ------------------------------------------------------------
[ -d "$APP_DIR" ]  || die "部署目录不存在：$APP_DIR"
[ -f "$PKG" ]      || die "安装包不存在：$PKG（请先在网页终端上传新包）"
command -v "$PYTHON" >/dev/null 2>&1 || die "找不到解释器 $PYTHON（请先安装 Python 3.10+）"

if [ ! -f "$PKG" ]; then die "包文件缺失"; fi
say "使用安装包：$PKG（$(du -h "$PKG" | cut -f1)）"

# ---- 1. 停旧服务（只杀自己记录的 PID）--------------------------------------
if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        say "停止旧服务（PID $OLD_PID）"
        kill "$OLD_PID" 2>/dev/null || true
        # 最多等 5 秒优雅退出
        for _ in 1 2 3 4 5; do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$OLD_PID" 2>/dev/null; then
            warn "进程未响应，强制结束（仍是自己的 PID，不影响他人）"
            kill -9 "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
        ok "旧服务已停止"
    else
        warn "PID 文件存在但进程已不在，跳过停止步骤"
    fi
else
    warn "未找到 $PID_FILE，跳过停止步骤（可能是首次部署）"
fi

# ---- 2. 备份当前版本（出问题可回滚）----------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${APP_DIR}-backup-$STAMP.tar.gz"
say "备份当前版本 → $BACKUP"
# 单行写法，避免续行符被意外破坏
tar -czf "$BACKUP" -C "$APP_DIR" --exclude='server.log' --exclude='server.pid' --exclude='__pycache__' . 2>/dev/null || warn "备份失败（不影响继续更新）"
[ -f "$BACKUP" ] && ok "备份完成：$BACKUP"

# ---- 3. 覆盖代码（--overwrite 直接盖掉旧文件，保留 server.pid/log）---------
say "解压新版本到 $APP_DIR"
# 注意：tar 选项写在一行内，避免续行符被行尾空格破坏
sudo tar -xzf "$PKG" -C "$APP_DIR" --strip-components=1 --overwrite --exclude='server.pid' --exclude='server.log'
ok "代码已更新"

# ---- 4. 回归测试（可选）-----------------------------------------------------
if [ "$VERIFY" = "1" ] && [ -f "$APP_DIR/tests/run_tests.py" ]; then
    say "跑回归测试（18 套件，约 40 秒；加 --no-verify 可跳过）"
    if (cd "$APP_DIR" && "$PYTHON" -B tests/run_tests.py >/tmp/verify.log 2>&1); then
        ok "回归测试全绿"
    else
        warn "回归测试未全绿，最近 15 行输出："
        tail -15 /tmp/verify.log
        warn "更新已覆盖，但测试未通过——请检查后再决定是否保留"
    fi
fi

# ---- 5. 重启服务 ------------------------------------------------------------
say "启动服务（$PYTHON, 端口 $PORT）"
cd "$APP_DIR"
PORT="$PORT" nohup "$PYTHON" -u -B server.py > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 3

if ! kill -0 "$NEW_PID" 2>/dev/null; then
    warn "服务未能启动，日志尾部："
    tail -20 "$LOG_FILE" 2>/dev/null || true
    die "启动失败（可回滚：sudo tar -xzf $BACKUP -C $APP_DIR --strip-components=1 --overwrite）"
fi
ok "服务已启动（PID $NEW_PID）"

# ---- 6. 自检 ----------------------------------------------------------------
say "自检：本机 HTTP 探测"
if curl -m 5 -sfI "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
    ok "本机访问正常：http://127.0.0.1:$PORT/"
else
    warn "本机探测未通过，日志尾部："
    tail -20 "$LOG_FILE" 2>/dev/null || true
fi

# ---- 7. 汇总 ----------------------------------------------------------------
echo
ok "更新完成"
echo "  部署目录：$APP_DIR"
echo "  进程 PID：$NEW_PID（记录于 $PID_FILE）"
echo "  日志文件：$LOG_FILE"
echo "  访问地址：http://<公网IP>:$PORT/"
echo "  备份文件：$BACKUP"
echo
echo "  停止服务：kill \$(cat $PID_FILE)"
echo "  查看日志：tail -f $LOG_FILE"
