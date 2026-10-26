#!/bin/bash
# 停止 CoreS3 演示：双击本文件，或在面板里点「关闭上位机」。
# 会先让上位机自己退出（/api/stop 会跑清理、释放串口），再兜底杀占用串口的进程。
set -u

cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

URL="http://127.0.0.1:8787/"

echo "== 停止 CoreS3 演示 =="
if curl -s --max-time 2 "${URL}api/health" >/dev/null 2>&1; then
    echo "请求上位机退出（释放串口 + 清理执行端）…"
    curl -s --max-time 3 "${URL}api/stop" >/dev/null 2>&1 || true
    sleep 1
fi
sh tools/free_port.sh --kill >/dev/null 2>&1 || true
echo "已停止，串口已释放。重新启动：双击「启动演示.command」。"
if [ -t 0 ]; then
    read -n 1 -s -r -p "按任意键关闭本窗口…"
    echo
fi
