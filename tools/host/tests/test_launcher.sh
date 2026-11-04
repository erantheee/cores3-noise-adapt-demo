#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)

sh -n "$ROOT/启动演示.command" "$ROOT/tools/host_up.sh"

ARGS=$(
    cd "$ROOT"
    CORES3_PYTHON=/bin/echo CORES3_WEB_PORT=18999 sh tools/host_up.sh | tail -1
)
case " $ARGS " in
    *" --system-eq-app system "*) : ;;
    *) echo "默认启动参数缺少 系统音频执行端：$ARGS" >&2; exit 1 ;;
esac
case " $ARGS " in
    *" --stem-remix "*) : ;;
    *) echo "默认启动参数缺少实时分轨：$ARGS" >&2; exit 1 ;;
esac
case " $ARGS " in
    *" htdemucs_6s "*) echo "默认分轨必须用吞吐优先的四轨模型：$ARGS" >&2; exit 1 ;;
    *) : ;;
esac

NO_STEM_ARGS=$(
    cd "$ROOT"
    CORES3_PYTHON=/bin/echo CORES3_WEB_PORT=18999 sh tools/host_up.sh --no-stem-remix | tail -1
)
case " $NO_STEM_ARGS " in
    *" --no-stem-remix "*) : ;;
    *) echo "显式关闭分轨时必须原样透传：$NO_STEM_ARGS" >&2; exit 1 ;;
esac
case " $NO_STEM_ARGS " in
    *" --stem-remix "*) echo "关闭分轨时不得再次注入开启参数：$NO_STEM_ARGS" >&2; exit 1 ;;
    *) : ;;
esac

grep -Eq 'api/snapshot' "$ROOT/启动演示.command"
grep -Eq '"stem_stream".*true' "$ROOT/启动演示.command"
grep -q '四轨实时分轨' "$ROOT/启动演示.command"


APP_ARGS=$(
    cd "$ROOT"
    CORES3_PYTHON=/bin/echo CORES3_WEB_PORT=18999 sh tools/host_up.sh --system-eq-app com.tencent.QQMusicMac | tail -1
)
case " $APP_ARGS " in
    *" --system-eq-app system "*) echo "显式单应用不得被全局默认覆盖" >&2; exit 1 ;;
esac
case " $APP_ARGS " in
    *" --system-eq-app com.tencent.QQMusicMac "*) : ;;
    *) echo "显式单应用参数丢失" >&2; exit 1 ;;
esac
grep -q 'capture_scope' "$ROOT/启动演示.command"

echo "启动器回归测试通过：默认系统音频四轨；单应用与关闭分轨参数保留"
