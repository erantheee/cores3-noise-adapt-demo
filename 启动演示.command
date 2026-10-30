#!/bin/bash
# 一键启动 CoreS3 演示：在 Finder 里双击本文件即可（会用 Terminal 打开）。
#
# 为什么需要这个文件：上位机没启动时，S3/网页都没法把 Mac 上的程序拉起来。
# 所以"启动"只能放在 Mac 上；S3 屏上做的将是运行中的暂停/继续/关闭（下一步）。
set -u

cd "$(dirname "$0")"
# Finder 启动的 Terminal 不读 ~/.zshrc，补上常见的 Homebrew / 本地 python 路径。
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

URL="http://127.0.0.1:8787/"

echo "== CoreS3 演示启动 =="

# 已经在跑且是低延迟版：不重复启动（重复启动会互相抢串口）。
# 若检测到高延迟六分轨链，主动切回低延迟模式。
if curl -s --max-time 2 "${URL}api/health" >/dev/null 2>&1; then
    SNAPSHOT=$(curl -s --max-time 3 "${URL}api/snapshot" 2>/dev/null || true)
    if ! printf '%s' "$SNAPSHOT" | grep -Eq '"stem_stream"[[:space:]]*:[[:space:]]*true'; then
        echo "当前低延迟上位机已经在运行，直接打开面板。"
        open "$URL" >/dev/null 2>&1 || true
        exit 0
    fi
    echo "检测到高延迟六分轨链，正在关闭并切换到低延迟版…"
    curl -s --max-time 4 "${URL}api/stop" >/dev/null 2>&1 || true
    sleep 2
fi

echo "1/2 释放串口（可能在跑的上位机 / 录音工具）…"
sh tools/free_port.sh --kill >/dev/null 2>&1 || true

echo "2/2 启动上位机（低延迟 EQ + 对话门 + 声纹 + 训练数据）…"
if ! sh tools/host_up.sh --detach; then
    echo
    echo "启动失败。完整输出：sh tools/host_up.sh"
    echo "串口被占用：sh tools/free_port.sh --kill"
    if [ -t 0 ]; then read -n 1 -s -r -p "按任意键关闭本窗口…"; echo; fi
    exit 1
fi

open "$URL" >/dev/null 2>&1 || true
echo
echo "面板：$URL"
echo "暂停 / 继续 / 关闭：面板顶部的「演示控制」。"
echo "S3：USB 插着即运行，不需要单独启动。"
echo
if [ -t 0 ]; then
    read -n 1 -s -r -p "按任意键关闭本窗口（上位机继续在后台运行）…"
    echo
fi
