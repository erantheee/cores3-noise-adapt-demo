#!/bin/sh
# 停掉占用串口的进程。刷写、录音工具都需要独占 /dev/cu.usbmodem*。
#
# 为什么单独做成一个脚本：这件事已经坑过两次——刷固件时忘了停上位机，esptool 报
# "multiple access on port"；而在页面上点录音的人只会看到 Failed to fetch，
# 因为上位机已经被停掉了，却没人告诉他。
#
#   sh tools/free_port.sh          # 只看谁占着，不做任何事
#   sh tools/free_port.sh --kill   # 真的停掉
set -eu
PORT="${CORES3_PORT:-/dev/cu.usbmodem1101}"
KILL=0
[ "${1:-}" = "--kill" ] && KILL=1

if [ ! -e "$PORT" ]; then
    echo "端口不存在（设备没插，或编号变了：ls /dev/cu.usb*）"
    echo "  找过的路径：$PORT"
    exit 0
fi

PIDS=$(lsof "$PORT" 2>/dev/null | awk 'NR>1{print $2}' | sort -u || true)
if [ -z "$PIDS" ]; then
    echo "$PORT 空闲"
    exit 0
fi

echo "$PORT 被以下进程占用："
for pid in $PIDS; do
    echo "  PID $pid  $(ps -o command= -p "$pid" 2>/dev/null | cut -c1-90)"
done

if [ "$KILL" -eq 0 ]; then
    echo "（只查看。要停掉它们：sh tools/free_port.sh --kill）"
    exit 0
fi

for pid in $PIDS; do
    kill "$pid" 2>/dev/null || true
done
sleep 2
if lsof "$PORT" >/dev/null 2>&1; then
    echo "普通 kill 无效，强制停止"
    for pid in $PIDS; do kill -9 "$pid" 2>/dev/null || true; done
    sleep 1
fi
lsof "$PORT" >/dev/null 2>&1 && { echo "$PORT 仍被占用"; exit 1; }
echo "$PORT 已空闲"
