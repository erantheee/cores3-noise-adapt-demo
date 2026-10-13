#!/bin/sh
# 刷写 CoreS3 固件；默认使用 outputs/current，也可传入任意固件目录：
#   sh tools/flash.sh outputs/firmware-1c8d363
# 覆盖项：CORES3_PORT、ESPTOOL
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${1:-$ROOT/outputs/current}"
PORT="${CORES3_PORT:-/dev/cu.usbmodem1101}"
ESPTOOL="${ESPTOOL:-/Users/eranthe/Documents/Codex/2026-10-10/du/work/uv-cache/archive-v0/LSKE5uaT0ygsXUNE/bin/esptool}"

BOOT="$DIR/bootloader/bootloader.bin"
[ -f "$BOOT" ] || BOOT="$DIR/bootloader.bin"
TABLE="$DIR/partition_table/partition-table.bin"
[ -f "$TABLE" ] || TABLE="$DIR/partition-table.bin"
APP=""
for f in "$DIR"/*.bin; do
    [ -f "$f" ] || continue
    case "$f" in
        *bootloader*|*partition*) ;;
        *) APP="$f" ;;
    esac
done
if [ -z "$APP" ] || [ ! -f "$BOOT" ] || [ ! -f "$TABLE" ]; then
    echo "找不到完整固件文件: $DIR"
    exit 1
fi

echo "Port: $PORT"
echo "App:  $APP"
"$ESPTOOL" --chip esp32s3 --port "$PORT" --baud 921600 write-flash \
    --flash-mode dio --flash-freq 80m --flash-size 16MB \
    0x0 "$BOOT" 0x8000 "$TABLE" 0x10000 "$APP"
echo "完成。日志：sh tools/monitor.sh"

