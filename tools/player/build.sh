#!/bin/sh
# 构建 Mac 播放与执行端（tools/player/player）。
#
# 为什么需要构建：macOS 上能做实时多段 EQ 的只有 AVAudioEngine + AVAudioUnitEQ，而
# Python 这边没有 PyObjC。所以播放器是一个用 swiftc 编译的命令行程序，由上位机驱动。
#
#   sh tools/player/build.sh
#
# 输出：tools/player/player（已在 .gitignore 里，不提交）
set -e

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SWIFTC="${SWIFTC:-swiftc}"

command -v "$SWIFTC" >/dev/null 2>&1 || {
    echo "找不到 swiftc。装 Xcode 命令行工具即可：xcode-select --install" >&2
    exit 1
}

echo "编译 $ROOT/player.swift …"
"$SWIFTC" -O "$ROOT/player.swift" -o "$ROOT/player"
echo "已构建：$ROOT/player"
echo "曲库：$ROOT/tracks.json（顶部 --tracks 可覆盖）"
