#!/bin/sh
# 本地编译/刷写模型基准固件。
#
# 环境细节抄自 cores3-noise-adapt-demo/tools/build.sh：
#   * 系统 python3 是 3.14，而 IDF 的 venv 建在 3.12 上，必须把 python3.12 放到 PATH 最前面
#     并显式指定 IDF_PYTHON_ENV_PATH，否则报找不到 idf5.4_py3.14_env
#   * export.sh 的输出被重定向后，失败只剩一句 "command not found: idf.py"
#
# 用法：
#   sh build.sh          # 只编译
#   sh build.sh flash    # 编译后刷入，并读回 8 秒启动输出
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
IDF_DIR="/Users/eranthe/esp/esp-idf"
IDF_PY_ENV="/Users/eranthe/.espressif/python_env/idf5.4_py3.12_env"
PY312_DIR="/Users/eranthe/.local/bin"

[ -f "$IDF_DIR/export.sh" ] || { echo "找不到 ESP-IDF：$IDF_DIR" >&2; exit 1; }
[ -d "$IDF_PY_ENV" ] || { echo "找不到 IDF python 环境：$IDF_PY_ENV" >&2; exit 1; }

PATH="$PY312_DIR:$PATH"
IDF_PYTHON_ENV_PATH="$IDF_PY_ENV"
export PATH IDF_PYTHON_ENV_PATH
. "$IDF_DIR/export.sh" >/dev/null

cd "$ROOT"
idf.py build

if [ "$#" -ge 1 ] && [ "$1" = "flash" ]; then
    PORT="$CORES3_PORT"
    if [ -z "$PORT" ]; then PORT="$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)"; fi
    [ -n "$PORT" ] || { echo "没有找到 /dev/cu.usbmodem* 设备" >&2; exit 1; }
    idf.py -p "$PORT" flash
    echo "--- 读回 8 秒启动输出 ---"
    "$IDF_PY_ENV/bin/python" "$ROOT/read_serial.py" --port "$PORT" --seconds 8 || true
fi
