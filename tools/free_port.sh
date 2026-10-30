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
KILL=0
[ "${1:-}" = "--kill" ] && KILL=1

# 端口不能写死：CoreS3 在不同机器/不同插拔顺序下会在 usbmodem101 与 usbmodem1101
# 之间变号。2026-10-30 真机是 usbmodem101，而旧默认值 1101 让这个脚本一路报
# 「端口不存在」然后 exit 0 —— 刷机/录音前"释放串口"这件事事实上没发生。
# 现在：显式 CORES3_PORT 优先；否则自动找，恰好一个就用它，多个就报错要求指定。
if [ -n "${CORES3_PORT:-}" ]; then
    PORT="$CORES3_PORT"
else
    PORT=""
    CANDIDATES=""
    for candidate in /dev/cu.usbmodem*; do
        [ -e "$candidate" ] || continue
        CANDIDATES="$CANDIDATES $candidate"
        [ -z "$PORT" ] && PORT="$candidate"
    done
    FOUND=0
    for candidate in $CANDIDATES; do FOUND=$((FOUND + 1)); done
    if [ "$FOUND" -gt 1 ]; then
        echo "找到多个串口设备，请用 CORES3_PORT 指定：$CANDIDATES"
        exit 1
    fi
fi

if [ -z "$PORT" ] || [ ! -e "$PORT" ]; then
    echo "端口不存在（设备没插，或编号变了：ls /dev/cu.usb*）"
    [ -z "$PORT" ] || echo "  找过的路径：$PORT"
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
