#!/bin/sh
# 本地编译 CoreS3 固件，不走 GitHub CI。
#
# 为什么需要这个脚本：CI 一轮构建要 6-7 分钟，改一行调音逻辑也要等一整轮。这台 Mac 上
# 已经装好 ESP-IDF v5.4.1 和 xtensa 工具链，本地首次全量编译约 3 分钟，之后改一处只是
# 几秒的增量编译。
#
# 两个环境细节（都踩过）：
#   * 系统的 python3 是 3.14，而 IDF 的 venv 建在 3.12 上。export.sh 按 python3 的版本
#     去找 idf5.4_py3.14_env，找不到就报错退出。把 python3.12 放到 PATH 最前面，并显式
#     指定 IDF_PYTHON_ENV_PATH，才会用回已经装好的那个 venv。
#   * export.sh 的输出一旦被重定向掉，失败就只剩 "command not found: idf.py"，看不出原因。
#
# 用法：
#   sh tools/build.sh          # 只编译
#   sh tools/build.sh flash    # 编译后刷入（复用 tools/flash.sh build）
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IDF_DIR="${IDF_DIR:-/Users/eranthe/esp/esp-idf}"
IDF_PY_ENV="${IDF_PY_ENV:-/Users/eranthe/.espressif/python_env/idf5.4_py3.12_env}"
PY312_DIR="${PY312_DIR:-/Users/eranthe/.local/bin}"

[ -f "$IDF_DIR/export.sh" ] || { echo "找不到 ESP-IDF：$IDF_DIR" >&2; exit 1; }
[ -d "$IDF_PY_ENV" ] || { echo "找不到 IDF python 环境：$IDF_PY_ENV" >&2; exit 1; }

PATH="$PY312_DIR:$PATH"
IDF_PYTHON_ENV_PATH="$IDF_PY_ENV"
export PATH IDF_PYTHON_ENV_PATH
. "$IDF_DIR/export.sh" >/dev/null

cd "$ROOT"
idf.py build

if [ "${1:-}" = "flash" ]; then
    if [ -z "${CORES3_PORT:-}" ]; then
        CORES3_PORT="$(ls /dev/cu.usbmodem* 2>/dev/null | head -1 || true)"
        [ -n "$CORES3_PORT" ] || { echo "没有找到 /dev/cu.usbmodem* 设备" >&2; exit 1; }
    fi
    CORES3_PORT="$CORES3_PORT" sh tools/flash.sh "$ROOT/build"
fi
