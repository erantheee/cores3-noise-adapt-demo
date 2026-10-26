# Session handoff — 2026-10-26（傍晚）

> 每轮收尾更新本文件（≤2.5KB）。新会话顺序：`AGENTS.md` → 本文件 → `docs/INDEX.md` → 按需读。
> 旧版交接已归档 `docs/handoff-archive-20261023.md`。

## 最新补充：千问接口切换（2026-10-26，优先于下方旧运行状态）
- 分支 `feature/apple-conversation-awareness` / HEAD `c8a27ea`；本轮未提交、未 push，保留原有未提交改动。
- LLM 改用千问 OpenAI Chat API：`qwen3.7-flash`、关闭 thinking、JSON；ASR 新 worker 使用 `qwen-audio-3.0-asr-flash`，WAV Base64 上传。provider 可分别回退；配置见 `docs/qianwen-api.md`。Key 仅在 gitignore 的 `.env`，0600。
- 云端转写失败（含部分轮次失败）保留错误并发布 unclear/close，不再当作空输出判 no；不改声纹算法/提示词/执行门/固件。
- 验证：新增 10 项测试、原 host 90 组/7917 断言通过；官方 3.83s 音频上传通过；生产提示词的人工构造问答/播音/身份不足分别 yes/no/unclear。
- 8787 当前 serial connected；LLM 千问 ready，ASR worker 就绪，EQ basic 和声纹可用；ambient-executor 仍 off。旧代理 403 不再是当前 LLM 状态。
- 待验：现场 ASR HTTP 400（空错误体）；有效示例语音通过、人工静音可复现，真实房间原因未定。下一步在面板说一句完整的话，检查转写/ASR 错误。不能宣称真人对话或听感验证成功。
- 启动用独立会话包裹 `sh tools/host_up.sh --detach --ambient-executor off`，防工具退出回收后台进程。不要重复占串口。

## 仓库状态
- 分支 `feature/apple-conversation-awareness`；工作树不干净：上一轮 host 修复 5 文件**仍未提交**
  （tools/host/{__main__.py,assistant.py,tests/test_host.py}、tools/voiceprint_worker.py）；
  本轮 D48 已按 firmware/docs 两个提交入库。远端未 push（origin 仍停在 66cdbd9）。
- 测试口径（2026-10-26 实测）：host 自测 90 组 / 7917 条（exit 0）；ctest 12/12、controller 113/113。

## 本轮内容（2026-10-26 下午 · D48 键盘/普通噪声特殊掩蔽）
- 用户反馈"EQ 基础状态遮蔽一般，尤其键盘噪声"→"没让你物理隔音，要你做特殊掩蔽"。
  定位（8787 快照）：键盘来时 event=keyboard、-42 dBFS、SONG_EQ、掩蔽推量≈0、内容曲线
  ±0.8 dB——D41 掩蔽只认人声，键盘没有任何掩蔽路径。
- 实现：`AdaptController::update()` 加 `keyboard_event` 参数；SONG_EQ 内两条掩蔽：
  ①普通噪声 宽带 +0.3、presence(1.6–4k) +1.0，深度 -54→-34；②键盘 宽带 +0.5、
  足迹段(1.0–3.4k，D17 质心 932 Hz) +1.5，深度 -52→-34。**深度独立慢平滑 400ms/1.2s**
  （每拍更新，flat/safety_hold 也衰减）。对话/自身语音=flat、安全保持只削不抬，豁免（D25）。
- **现场迭代**：第一版"拉爆"（足迹段 +4、696.5 Hz 起）→ 用户："人声完全变了、毛刺感极强"；
  第二版幅度砍约 1/3 + 足迹下沿上移 1 kHz + 深度慢平滑，已刷机（两次）。
- 8787 host 已按默认三件套重启（QQ音乐 systemeq basic / ambient-gate voice / voiceprint）。

## 未完成 / 下一步
- **⚠️ 待用户现场听感验收第二版**（敲键盘 + 稳态噪声）；不满意改 controller.cpp 的
  `kNoiseMask*` / `kKeyboardMask*` 常量，改后 ctest → free_port → flash → host_up。
- 键盘掩蔽在 SAFETY_HOLD 豁免：猛敲把 250 ms 平滑电平顶过 -30 dBFS 仍会有模式边界抽动
  （D38 既有）；第二版幅度小应大减，若仍刺耳可给 keyboard_event 加模式滞回。
- LLM API 403（10-26 14:2x 复现）：assistant error=HTTP 403，对话判定退化 local/unclear；
  代码层修不了，需充值/查代理。
- 上一轮 host 修复（环境声触发）仍未提交，建议下轮按惯例补提交。
- 遗留（不变）：A0 真人验收、A1 LLM 否决回写 S3、#38 房间本底、D47 三态阈值、
  6.5 vs 8.0 dB、声纹 self-veto 默认关、other 快锁。

## 当前环境（2026-10-26 15:2x）
- 8787 host 在跑（serial 真机）；S3 `/dev/cu.usbmodem101`；面板 http://127.0.0.1:8787/；
  systemeq basic depth 1.3（面板可调）；输出 Xiaomi 耳夹；LLM gpt-5.6-sol（403）、ASR qwen3。

## 千问 ASR 400 根因（2026-10-26 16:4x，只读诊断，未改代码）
- 结论：模型没坏。实测干净 TTS / -52 dBFS 弱语音 / SNR 0 dB 混噪 / 400 ms 短句均 200；HTTP 400 `{}` 只出现在窗口内无可识别人声（静音、键盘、房间噪声）。seg0001 最响 1.5 s（键盘）400、最安静 2.5 s 200（"你是写出来了。"）；本地 qwen3-asr 判断一致（噪声→"嗯。"）。
- 现场 15:36 会话 457 段：400=222、跳过=178、真实转写≈1 条（seg0129）；当前快照 segments=518/passed=30/skipped=173/filtered=315，asr_error 仍显示 HTTP 400。
- 根因在上位机门控：能量 VAD 把键盘瞬态当语音（设备 voice_likelihood 贡献 0 s）；`_MIN_SPEECH_S=1.0`（assistant.py:86）把 0.4–1.0 s 短句整段跳掉（seg0376 证据 0.84 s → 跳过）；任一切片 400 → 整轮 unclear 且跳过 LLM（:327），面板写"请求失败"（:284）；`qianwen_api.py:41` 丢弃错误体 → 现场只见空 400。
- 建议（未实施）：400 软降级为"无人声"；发送前加周期性/谱平坦度门（噪声<0.15、语音>0.25）；`_MIN_SPEECH_S` 降至 0.45–0.6 s；记录脱敏错误体。
- 证据：`记录/训练数据/20261026-153627/`（seg0001-3=400、seg0129=200、seg0376=短句被跳）。

## 已实施（2026-10-26 17:0x，未提交）
- ASR：`qianwen_api` 加 `ProviderHTTPError/AsrNoSpeech`（400=无人声）；worker 回 `transcript_nospeech`；host 按"这段没听出内容"处理（不再"请求失败"）；部分片段失败但拿到正文→"部分片段转写失败"。设备事件与 ASR 耦合：`_BLOCKING_EVENTS`（keyboard/wind/artifact）时段从语音证据里挖掉（`_event_blocked_regions/_subtract_intervals`），全被覆盖则跳过不调用。`_MIN_SPEECH_S 1.0→0.55`。
- 声纹：`--unknown-margin 0.15→0.30`（中间区分 unknown 不误判 other）；`--window-s 1.5→3.0`（现场实测 1.5 s 窗本人 0.65–0.75、噪声 0.65 不可分；3 s 窗本人 0.86–0.90、键盘/噪声 0.65–0.71）。
- 测试：host 92 组 / 7925 断言、qianwen 11 项全过；8787 已重启（17:07），现场已见"服务端未检出人声"不再写请求失败。
- **现场验收（17:20，重启后新会话 20261026-171849）**：self 触发 15 窗（0.89–0.98）；seg15 转出 `self：测试一下声纹，1234567`、seg14 `unknown：测试一下。 / self：要升温。` → LLM 判 yes/open（0.85–0.9）；噪声段显示“未检测到有效人声/服务端未检出人声”，不再出现“转写请求失败”；asr_error 空。
- **声纹已现场重注册（17:18）**：注册句 3 遍落在 `记录/训练数据/20261026-170705` seg0089/90/91，用 `tools/enroll_from_segments.py` 生成 `记录/声纹/self-device/live-*.wav`；旧目录（含 learned-prototype.npz）整体备份为 `记录/声纹/self-device-20261019`。离线校验：本人 0.95–0.98、同事 0.25–0.43、键盘噪声 0.50。host 已重启：voiceprint clips=3、updates=0、learned_from 空。待验：真人说话时快照 history 出现 self（≥0.8）且 ASR 正常出转写。

## 验证入口
```bash
cmake -S test -B build-host-tests && cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
python3 tools/host/tests/test_host.py
# 重刷（先停 host）：
CORES3_PORT=/dev/cu.usbmodem101 sh tools/free_port.sh --kill
sh tools/build.sh flash
sh tools/host_up.sh --detach
```
