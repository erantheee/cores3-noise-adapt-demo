#!/bin/sh
# 构建第三方播放器 system EQ 原型（QQ 音乐 tap → 24 段 EQ → 当前耳机）。
#   sh tools/systemeq/build.sh
# 输出：tools/systemeq/systemeq（已在 .gitignore 之外，仅本地构建）
set -e
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SWIFTC="${SWIFTC:-swiftc}"
command -v "$SWIFTC" >/dev/null 2>&1 || { echo "找不到 swiftc" >&2; exit 1; }
TARGET="${SWIFT_TARGET:-$(uname -m)-apple-macosx15.0}"
MODULE_CACHE="${TMPDIR:-/tmp}/cores3-swift-module-cache"
mkdir -p "$MODULE_CACHE"
"$SWIFTC" -target "$TARGET" -module-cache-path "$MODULE_CACHE" \
  -O "$ROOT/systemeq.swift" -o "$ROOT/systemeq" \
  -framework CoreAudio -framework AVFoundation -framework AppKit
echo "已构建：$ROOT/systemeq"
