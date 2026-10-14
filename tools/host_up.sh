#!/bin/sh
# 启动 CoreS3 上位机（面板跑在 http://127.0.0.1:8787/）
#
#   sh tools/host_up.sh                 # 真机（自动找端口）
#   sh tools/host_up.sh --mock          # 没有设备时先把界面跑起来
#   sh tools/host_up.sh --replay 记录/x.log --speed 4
#
# 覆盖项：
#   CORES3_PYTHON   指定解释器（必须带 pyserial）
#   CORES3_PORT     指定串口，等价于 --port
#   CORES3_WEB_PORT 面板端口，等价于 --web-port
set -e

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
UV_PY="/Users/eranthe/Documents/Codex/2026-10-10/du/work/uv-cache/archive-v0/LSKE5uaT0ygsXUNE/bin/python"

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

set -- --web-port "${CORES3_WEB_PORT:-8787}" "$@"
if [ -n "${CORES3_PORT:-}" ]; then
    case " $* " in
        *" --mock "*|*" --replay "*) : ;;
        *) set -- --port "$CORES3_PORT" "$@" ;;
    esac
fi

cd "$ROOT/tools"
exec "$PY" -u -m host "$@"
