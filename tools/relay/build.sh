#!/bin/sh
# 构建实时中继播放端（tools/relay/relaysink）。
#
# 为什么需要构建：macOS 上唯一能拿到"播放头真正播到哪一帧"的接口在 CoreAudio/AVFoundation 里，
# 而 Python 这边没有 PyObjC。上位机需要一个能**报出真实排队深度**的播放器，所以这里用 swiftc
# 编一个纯命令行程序，由 tools/host/audio_relay.py 驱动。
#
#   sh tools/relay/build.sh
#
# 输出：tools/relay/relaysink（已在 .gitignore 里，不提交）
set -e

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SWIFTC="${SWIFTC:-swiftc}"

command -v "$SWIFTC" >/dev/null 2>&1 || {
    echo "找不到 swiftc。装 Xcode 命令行工具即可：xcode-select --install" >&2
    exit 1
}

echo "编译 $ROOT/relaysink.swift …"
"$SWIFTC" -O "$ROOT/relaysink.swift" -o "$ROOT/relaysink"
echo "已构建：$ROOT/relaysink"
