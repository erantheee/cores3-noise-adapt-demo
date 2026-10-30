#!/bin/sh
# QQ Music -> process tap -> rolling six-stem remix -> current output device.
# Usage: sh tools/stem_remix/run_qq_stream.sh [environment]
set -e

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TOOLS=$(CDPATH= cd -- "$HERE/.." && pwd)
ENVIRONMENT=${1:-broadband}

case "$ENVIRONMENT" in
  quiet|broadband|keyboard|wind|babble) ;;
  *)
    echo "环境只接受 quiet / broadband / keyboard / wind / babble" >&2
    exit 2
    ;;
esac

sh "$TOOLS/systemeq/build.sh"
exec "$TOOLS/systemeq/systemeq" \
  --app com.tencent.QQMusicMac \
  --stem-worker "$HERE/stream_worker.py" \
  --stem-environment "$ENVIRONMENT" \
  --stem-device mps \
  --report 1
