# 千问 ASR 与对话判断接入

2026-10-26：仅更换上位机接口与模型，不改固件、声纹算法、对话提示词或执行门。

## 分工

- `qwen-audio-3.0-asr-flash`：PCM16 单声道封装成 WAV，以 Base64 Data URI 提交云端转写；不是通用对话判断模型。
- `qwen3.7-flash`：OpenAI Chat Completions 接口，`enable_thinking=false`，JSON 输出；沿用当前转写/声纹轮次/跨段上下文提示词。
- 原有语音证据筛选、身份约束、对话状态与迟到响应检查保持原样。API 报错/正文缺失/JSON 无效按原逻辑发布 `unclear/close`、source=local；最终是否执行仍由既有 gate 决定，不能把 API 调通当作耳机听感验证。

## 本地配置

在被 Git 忽略的根目录 `.env` 设置以下变量；Key 只保存在本机，权限设为 0600。

```sh
CORES3_LLM_PROVIDER=qianwen
CORES3_ASR_PROVIDER=qianwen
DASHSCOPE_API_KEY=你的Key
QIANWEN_LLM_MODEL=qwen3.7-flash
QIANWEN_ASR_MODEL=qwen-audio-3.0-asr-flash
QIANWEN_LLM_BASE_URL=https://maas.qianwenaiapi.com/compatible-mode/v1
QIANWEN_ASR_BASE_URL=https://maas.qianwenaiapi.com/api/v1
```

通过 `sh tools/host_up.sh --detach` 启动；已经运行时需先检查快照并保留原参数后重启。面板 `/api/snapshot` 的 `assistant.provider/asr_provider/model/asr_model` 应显示千问配置；`state=ready` 表示 LLM 探测成功，`asr_ready=true` 仅表示 worker 就绪，实际转写另行验证。

回退：把 `.env` 中 `CORES3_LLM_PROVIDER` 改为 `anthropic`、`CORES3_ASR_PROVIDER` 改为 `local`，保留原 `ANTHROPIC_*` 与 `.asr-venv` 后重启。也可单独切换其中一路；原代理若仍 403 则回退后仍不可用。

不设置 provider 时保持旧配置兼容。云端转写会上传筛选后的语音片段；失败不自动切换其他服务。ASR 30 秒超时、LLM 45 秒超时；仍使用现有队列与轮次检查，非流式接口的实际对话延迟需现场测量。

## 验证

```sh
python3 tools/host/tests/test_qianwen_api.py
python3 tools/host/tests/test_host.py
```

- 新增离线接口测试：10 项，通过。覆盖请求格式、WAV、响应层级、空正文、截断、错误脱敏、配置选择、生产判断路径及整段/部分轮次 ASR 失败不误判为非对话。
- 原有上位机测试：90 组 / 7917 条断言，通过。
- 真实接口：官方 3.83 秒 WAV 经新 worker Base64 上传成功；生产提示词下，人工构造的双方问答=yes、新闻播音=no、单句且身份不足=unclear。
- 以上不代表真实房间准确率、硬件闭环或听感验收；本轮未改固件，无需重跑 C++/刷机。

## 官方参考

- https://platform.qianwenai.com/docs/api-reference/speech-recognition/fun-asr-flash/http-api
- https://platform.qianwenai.com/docs/api-reference/chat/openai-chat
- https://platform.qianwenai.com/docs/developer-guides/text-generation/thinking

## 最终运行状态与待验证边界

本机 8787 已切换：provider/asr_provider=qianwen，LLM state=ready，ASR worker 就绪；串口 connected=true，QQMusic basic EQ 与声纹仍可用。沿用原 ambient-executor=off，没有启用环境声播放。

现场片段出现 HTTP 400，返回空 JSON 无具体原因；同配置有效官方语音复测成功，人工静音 0.1/0.3/1/2 秒均复现 400。不能据此断言所有现场失败都是静音，待真人说话验证。错误明确显示为转写请求失败/不确定，不伪装成无语音或确定非对话；部分轮次失败不调用 LLM。

启动脚本在工具会话退出时曾被连带回收；本次用 start_new_session 启动同一 host_up.sh --detach 后，已跨工具调用验证存活。尚未提交或推送代码。
