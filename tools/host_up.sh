#!/bin/sh
# 启动 CoreS3 上位机（面板跑在 http://127.0.0.1:8787/）
#
#   sh tools/host_up.sh                 # 真机，前台运行（Ctrl+C 停止）
#   sh tools/host_up.sh --detach        # 脱离会话在后台运行（推荐：会话被回收也不会带走它）
#   sh tools/host_up.sh --status        # 只看它是否在运行
#   sh tools/host_up.sh --mock          # 没有设备时先把界面跑起来
#   sh tools/host_up.sh --replay 记录/x.log --speed 4
#
# 覆盖项：
#   CORES3_PYTHON   指定解释器（必须带 pyserial）
#   CORES3_PORT     指定串口，等价于 --port
#   CORES3_WEB_PORT 面板端口，等价于 --web-port
#   CORES3_HOST_LOG 后台模式的日志路径（默认 /tmp/cores3host/host.log）
set -e

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WEB_PORT="${CORES3_WEB_PORT:-8787}"
URL="http://127.0.0.1:$WEB_PORT/"
LOG="${CORES3_HOST_LOG:-/tmp/cores3host/host.log}"
UV_PY="/Users/eranthe/Documents/Codex/2026-10-10/du/work/uv-cache/archive-v0/LSKE5uaT0ygsXUNE/bin/python"

show_status() {
    if curl -s --max-time 3 "${URL}api/health" >/dev/null 2>&1; then
        echo "上位机在运行：$URL"
        curl -s --max-time 3 "${URL}api/health"; echo
        return 0
    fi
    echo "上位机没有运行（$URL 无响应）"
    return 1
}

case "${1:-}" in
    --status)
        show_status || true
        exit 0
        ;;
esac

# 串口模式需要 pyserial；模拟/回放模式不需要。所以先按"谁能跑"排序，再由 host 自己
# 在真正缺依赖时报错退出码 3——这里不做判断，避免把"缺依赖"和"没设备"混成一句话。
PY=""
for candidate in "${CORES3_PYTHON:-}" "$UV_PY" python3; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        if "$candidate" -c 'import serial' >/dev/null 2>&1; then
            break
        fi
    fi
done
[ -n "$PY" ] || { echo "找不到可用的 Python 解释器，请用 CORES3_PYTHON 指定"; exit 1; }

DETACH=0
if [ "${1:-}" = "--detach" ]; then
    DETACH=1
    shift
fi

set -- --web-port "$WEB_PORT" "$@"
if [ -n "${CORES3_PORT:-}" ]; then
    case " $* " in
        *" --mock "*|*" --replay "*) : ;;
        *) set -- --port "$CORES3_PORT" "$@" ;;
    esac
fi

cd "$ROOT/tools"

if [ "$DETACH" -eq 0 ]; then
    exec "$PY" -u -m host "$@"
fi

# 后台模式。为什么要脱离会话：前台/普通后台任务会随会话的进程树一起被回收，
# 而症状是页面点录音报 `Failed to fetch`——请求根本没发出去，看起来像录音功能坏了。
if show_status >/dev/null 2>&1; then
    echo "上位机已经在运行，不重复启动。"
    show_status
    exit 0
fi

mkdir -p "$(dirname "$LOG")"
if command -v setsid >/dev/null 2>&1; then
    nohup setsid "$PY" -u -m host "$@" >>"$LOG" 2>&1 &
else
    nohup "$PY" -u -m host "$@" >>"$LOG" 2>&1 &
fi
disown 2>/dev/null || true

for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 2
    if show_status >/dev/null 2>&1; then
        show_status
        echo "日志：$LOG"
        exit 0
    fi
done

echo "启动后 20 秒仍未就绪，日志尾部：" >&2
tail -15 "$LOG" >&2
exit 1
