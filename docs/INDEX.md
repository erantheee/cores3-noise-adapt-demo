# docs/ 索引（2026-10-22）

> 顺序：`AGENTS.md` → `docs/handoff.md` → 本索引 → 按需点开具体文档。
> 大文档一律 grep/分段读，**不要整篇读**。表格大小为当时字节数，会随提交变化。

| 文档 | 大小 | 角色 | 阅读方式 |
|---|---|---|---|
| `product-alignment.md` | 6 KB | **唯一产品口径**（分工/行为/边界） | 精读 |
| `goals.md` | 5 KB | 目标与验收表 | 精读 |
| `handoff.md` | 2.5 KB | 跨会话状态/下一步 | 每次先读；收尾更新（旧版归档在 `handoff-archive-20261023.md`） |
| `plan-status.md` | 70 KB | 状态总表（#1–88+）+ 真机数字 + 下一步 | 只读「状态总表」+「下一步」；数字 grep 关键字 |
| `decisions.md` | 103 KB | D1–D52 决策日志（最新 D52） | `grep -n '^## D' docs/decisions.md` 按编号读单条 |
| 根 `README.md` | 57 KB | 索引 + 按日期变更叙事 | 只读「当前闭环」「真机踩到的坑」「构建」「主机单元测试」 |
| `conversation-prediction.md` | 7 KB | 对话预判设计 | 按需 |
| 根 `TODOS.md` | 12 KB | 待办（A0/A1 优先） | 读开头 A0/A1 + P1 |
| `apple-conversation-awareness.md` | 40 KB | Apple 对话感知研究快照（已迁入主工程，历史参考） | **归档级**：用户点名才看 |
| `edge-feasibility.md` | 14 KB | 一次性可行性论证 | **归档级** |
| `integration-plan.md` | 11 KB | 2026-10-16 融合计划（已完成） | **归档级** |

## 已知矛盾（引用前先复核）
- 测试条数：三处已同步到 2026-10-26 实测（C++ 12 套件、host 90 组/7928 条、渲染约 80/0）；仍以实际运行输出为准。
- 环境声执行端默认：D24（airpods）vs D30（off）→ 当前 off。
- 背景人声动作：D16/D26（人声靠前）已被 D41 掩蔽覆盖。
- 真机数字必须带固件版本；设备录音 16 kHz 上限；AirPods yaw = 头朝向 ≠ DOA。
