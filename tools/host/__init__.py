"""CoreS3 上位机。

模块划分刻意保持四层互不越界：

    protocol.py   串口文本 → 结构化帧（不依赖 pyserial，不碰 IO）
    state.py      帧流 → "设备现在处于什么状态"（含丢帧、陈旧、录音态）
    sources.py    数据从哪来（真机串口 / 历史回放 / 模拟）
    server.py     HTTP + SSE + 回控白名单

界面（web/index.html）只渲染 server.py 给出的快照，不做任何判断——
判断错了才会让数字说谎，所以判断全部留在可单测的 Python 里。
"""

__all__ = ["protocol", "state", "sources", "server", "simulating"]
