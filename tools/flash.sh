#!/bin/sh
# 刷写 CoreS3 固件；默认使用 outputs/current，也可传入任意固件目录：
#   sh tools/flash.sh outputs/firmware-9b3a057
# 覆盖项：CORES3_PORT、ESPTOOL
#
# 选片规则是显式的：先认 CI 产物的名字 cores3_noise_adapt_demo.bin，再认 app.bin，
# 其余 *.bin 只有在**唯一**一个候选时才使用，否则报错退出。
#
# 为什么要这么严：原来是把目录里所有非 bootloader/partition 的 *.bin 循环一遍、留下最后
# 一个。目录里同时存在新旧两个 app 时（例如 CI 产物和 outputs/current 的旧文件），它会
# 静默刷错固件——而屏幕上不会有任何区别，因为两边都跑得起来。烧错固件再排查是很贵的事，
# 所以这里宁可拒绝执行。
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${1:-$ROOT/outputs/current}"
PORT="${CORES3_PORT:-/dev/cu.usbmodem1101}"
ESPTOOL="${ESPTOOL:-/Users/eranthe/Documents/Codex/2026-10-10/du/work/uv-cache/archive-v0/LSKE5uaT0ygsXUNE/bin/esptool}"

BOOT=""
for candidate in "$DIR/bootloader/bootloader.bin" "$DIR/bootloader.bin"; do
    [ -f "$candidate" ] && BOOT="$candidate" && break
done
TABLE=""
for candidate in "$DIR/partition_table/partition-table.bin" "$DIR/partition-table.bin"; do
    [ -f "$candidate" ] && TABLE="$candidate" && break
done

APP=""
for candidate in "$DIR/cores3_noise_adapt_demo.bin" "$DIR/app.bin"; do
    [ -f "$candidate" ] && APP="$candidate" && break
done

if [ -z "$APP" ]; then
    # 没有已知名字时，只在候选唯一的情况下接受。
    count=0
    unique=""
    for f in "$DIR"/*.bin; do
        [ -f "$f" ] || continue
        case "$f" in
            *bootloader*|*partition*) continue ;;
        esac
        count=$((count + 1))
        unique="$f"
    done
    if [ "$count" -eq 1 ]; then
        APP="$unique"
    elif [ "$count" -eq 0 ]; then
        echo "找不到 app 固件: $DIR" >&2
        exit 1
    else
        echo "无法确定要刷哪个 app 固件，$DIR 里有 $count 个候选：" >&2
        for f in "$DIR"/*.bin; do
            case "$f" in *bootloader*|*partition*) continue ;; esac
            echo "  $f" >&2
        done
        echo "请指定确切的固件目录，或把目录清理成只有一个 app。" >&2
        exit 1
    fi
fi

if [ ! -f "$BOOT" ] || [ ! -f "$TABLE" ]; then
    echo "找不到完整固件文件: $DIR" >&2
    echo "  bootloader: ${BOOT:-缺失}" >&2
    echo "  partition:  ${TABLE:-缺失}" >&2
    exit 1
fi

# 打印指纹，便于和 CI 产物核对。刷错固件时这是唯一能事后发现的地方。
if command -v shasum >/dev/null 2>&1; then
    HASH="$(shasum -a 256 "$APP" | cut -d' ' -f1)"
elif command -v sha256sum >/dev/null 2>&1; then
    HASH="$(sha256sum "$APP" | cut -d' ' -f1)"
else
    HASH="(无 shasum/sha256sum)"
fi

echo "Port:   $PORT"
echo "Boot:   $BOOT"
echo "Table:  $TABLE"
echo "App:    $APP"
echo "SHA256: $HASH"

# 三个区域**分开写、逐个校验**。2026-10-19 实测：esptool v5.5.0 把三个
# "地址 文件" 对放在同一条 write-flash 里时，日志会正常打印每个区域、也报
# "Hash of data verified"，但回读 0x8000 是全 0xFF、0x10000 还是上一个固件——
# 分区表没写进去，启动报 "No bootable app partitions"，设备停在下载模式。
# 分开写之后回读逐字节一致。刷机脚本宁可慢几秒，也不能"报告成功但没写进去"。
for pair in "0x0 $BOOT" "0x8000 $TABLE" "0x10000 $APP"; do
    # shellcheck disable=SC2086
    set -- $pair
    "$ESPTOOL" --chip esp32s3 --port "$PORT" --baud 921600 write-flash \
        --flash-mode dio --flash-freq 80m --flash-size 16MB "$1" "$2"
done
echo "完成。日志：sh tools/monitor.sh"
