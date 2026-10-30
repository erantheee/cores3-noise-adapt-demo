#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)

sh -n "$ROOT/启动演示.command" "$ROOT/tools/host_up.sh"

ARGS=$(
    cd "$ROOT"
    CORES3_PYTHON=/bin/echo CORES3_WEB_PORT=18999 sh tools/host_up.sh | tail -1
)
case " $ARGS " in
    *" --system-eq-app com.tencent.QQMusicMac "*) : ;;
    *) echo "默认启动参数缺少 QQ 音乐低延迟执行端：$ARGS" >&2; exit 1 ;;
esac
case " $ARGS " in
    *" --stem-remix "*) echo "默认不得开启高延迟六分轨：$ARGS" >&2; exit 1 ;;
    *) : ;;
esac

grep -Eq 'api/snapshot' "$ROOT/启动演示.command"
grep -Eq '"stem_stream".*true' "$ROOT/启动演示.command"
grep -q '低延迟' "$ROOT/启动演示.command"

echo "启动器回归测试通过：默认低延迟，且能替换高延迟上位机"
