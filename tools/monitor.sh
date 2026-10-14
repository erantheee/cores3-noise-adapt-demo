#!/bin/sh
# 查看 CoreS3 串口日志（Ctrl+C 退出）
# 覆盖项：CORES3_PORT（默认 /dev/cu.usbmodem1101）、CORES3_PYTHON（需带 pyserial）
PORT="${CORES3_PORT:-/dev/cu.usbmodem1101}"
BAUD="${CORES3_BAUD:-115200}"
UV_PY="/Users/eranthe/Documents/Codex/2026-10-10/du/work/uv-cache/archive-v0/LSKE5uaT0ygsXUNE/bin/python"

PY=""
for p in "${CORES3_PYTHON:-}" "$UV_PY" python3; do
    [ -n "$p" ] || continue
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import serial' >/dev/null 2>&1; then
        PY="$p"
        break
    fi
done
[ -n "$PY" ] || { echo "找不到带 pyserial 的 Python，请用 CORES3_PYTHON 指定"; exit 1; }

exec "$PY" - "$PORT" "$BAUD" <<'PYEOF'
import sys
import serial

port, baud = sys.argv[1], int(sys.argv[2])
ser = serial.Serial(port=port, baudrate=baud, timeout=0.2)
ser.dtr = False
ser.rts = False
print(f"listening on {port} @ {baud}, Ctrl+C to exit", flush=True)
try:
    while True:
        data = ser.read(4096)
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
except KeyboardInterrupt:
    pass
PYEOF

