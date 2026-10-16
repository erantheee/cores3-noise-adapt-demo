# 实验代码融合进主工程的方案

最后更新：2026-10-16

**进度：第一轮 ⏸ 未开始 ｜ 第二轮 ✅ 已完成并真机实测（2026-10-16）｜ 第三轮 ⏸ 未开始。**
第二轮的实测数字、四个坑、以及"模型在真实房间里还不行"这条结论，都写在
[`plan-status.md` 第 29 项](plan-status.md)里。**第三轮现在多了一个前置条件**：模型在真实房间
本底上把几乎所有声音都判成 `other`（第 29 项实测），接决策之前先解决它。

这一轮做的时候发现，第二节决策 2 说"运动两位必须保持 0"是对的，但**同一类错位还有两处没被
发现**（时域闸门改变了 `voice_likelihood`、时间 5 维用了错的基线）。两处都已修，见 plan-status
第 29a/29b 项。教训是：写"必须保持"的地方要连**怎么发现它变了**一起写。

`experiments/` 下的三个独立工程（`ear-context`、`listening-model`、`model-bench`）已经分别验证了
端侧模型、六轴采集和耳戴整合。这份文档说清楚**哪些进主工程、哪些不进、按什么顺序**，以及
融合时会踩到的坑。

前提：主工程 `main/` 是唯一的产品 demo，它已经测过的东西（100 ms 控制节拍、24 段 EQ、
安全保持、端到端延迟、设备端录音、串口协议）**一条都不能丢**。融合是加法，不是替换。

---

## 一、融合面

| 实验产物 | 位置 | 主工程现状 | 处理 |
|---|---|---|---|
| `imu.cpp` / `imu.hpp`（BMI270 + data-ready 修复） | `ear-context/main/` | **完全没有 I2C / IMU** | ✅ 进 |
| `window.hpp`（92 维窗口） | `ear-context/main/` | 没有 | ✅ 进（上模型才需要） |
| `listening_model.h`（v0.2-s3，3,657 参数） | `model-bench/main/` | 没有端侧 NN | ✅ 进 |
| `check_font()` 开机自检 | `ear-context/main/ui.cpp` | 没有 | ✅ 进（见坑 5） |
| `tools/font_probe.py` | 仓库根 | 没有 | ✅ 已在根 `tools/` |
| `ui.cpp` / `ui.hpp` + `ear_font16/24.c` | `ear-context/main/` | 有 `demo_ui.cpp` + `lv_font_ui_16.c` | ❌ **不进**（见决策 3） |
| `app_main.cpp`（150 行） | `ear-context/main/` | 636 行，是完整 demo | ❌ **不进**，用它的结构 |
| `collect_selfvoice.py` / `selfvoice_report.py` | `ear-context/` | — | ⏸ 留在实验，等轴2 结论 |
| `touch_diag.cpp` / `touch_diag.hpp` | 已删除 | — | ❌ 排查用，已撤 |

---

## 二、三个必须先定的决策

### 决策 1：模型输出接不接决策？

- **A. 只进 UI + 串口遥测**（零风险，不改任何行为）
- **B. 接进 `AdaptController`，影响 `FLAT` / `SONG_EQ` / `SAFETY_HOLD`**

**建议 B，但只接一个用途：`horn` → `SAFETY_HOLD`。**

理由：`plan-status.md` 第 23 项自己写着"高噪声非人声进入 `SAFETY_HOLD`；**车声分类仍未实现**"。
模型 9 类里正好有 `horn`（鸣笛）。这是**唯一一个产品上明确缺、且模型正好有**的能力。
其余 8 类先只显示，不进决策——它们没有一个有对应的产品动作，硬接就是凭感觉加逻辑。

代价：`AdaptController::update(const AnalysisFrame&, float dt_ms)` 要扩展签名
（加一个可选的场景输入），并且要补单测。这一步不能和第一、二轮混做。

### 决策 2：真 IMU 数据进不进模型输入？

**不能，这一条是硬的。**

`window.hpp` 里这两行是刻意的：

```cpp
// v0.2 was trained with motion=0. Raw IMU must not enter these inputs yet.
out[85]=out[86]=0;
```

`make_dataset.py` 训练时传的是 `motion=None`，motion 特征全程为 0。所以把真六轴填进
`out[85]/out[86]` 是**分布外输入**，模型不会报错，只会静默劣化——这个项目里最贵的一类错误。

**结论：IMU 先只喂控制器、UI 和遥测；要进模型必须先重训**（带真运动量的数据重训一轮）。
在那之前，`out[85]/[86]` 保持 0 不动。

### 决策 3：UI 保哪一套？

**保留 `demo_ui.cpp`。** 它 234 行，每个数字都有实测出处（`demo_ui_update()` 的入参就是
`AnalysisFrame` + `EarbudCommand` + `MacAudioRoute` + 处理耗时 + 空闲堆）。换掉是净损失。

做法是**加一个格子**，不是换一套：
- 环境情境（9 类里最高分的那个 + 分数）
- 运动状态（六轴是否在采、活动量）

`ear-context` 那套 `ui.cpp` 的价值是验证了"两页 + 中文字库能在真机上跑"，这个结论已经拿到，
代码本身不用进。

---

## 三、融合时会踩的坑

**坑 1：BMI270 驱动现在住在实验工程里。**
`experiments/ear-context/components/bmi270/`，而根工程**没有 `components/` 目录**。
要挪到 `<root>/components/bmi270/`（BSD-3-Clause，随附 LICENSE），然后让 `ear-context`
用 `EXTRA_COMPONENT_DIRS` 指回根目录，**避免两份副本各自漂移**。

**坑 2：主工程是单任务，IMU 需要 100 Hz。**
`main/app_main.cpp` 只有一个 `processing_task`（priority 6，core 1，12288 栈），100 ms 一拍。
100 ms 轮询六轴必丢样本。要单独起一个 IMU 任务（实验里是 priority 5、**core 0**）。
**别放 core 1**——会和 `processing_task` 和 LVGL 任务抢。

**坑 3：主工程现在没有任何 I2C。**
`imu.cpp` 走的是 `bsp_i2c_init()` + `i2c_master_get_bus_handle(BSP_I2C_NUM, &bus)`。
主工程已经用了 BSP（`bsp_display_start`、`bsp_audio_init`），所以不引入新依赖，但要注意
**触摸（FT5x06）和 BMI270 在同一条 I2C 总线上**——这也是 `ear-context` 上验证过的组合。

**坑 4：模型头文件是生成物，别手改。**
`listening_model.h` 由 `listening-model/scripts/export_c.py` 生成，约 3000 行权重。
融合后应该生成到 `main/listening_model.h`（`--header` 已经参数化）。改模型只能重跑导出，
不能在头文件里手改。

**坑 5：任何新字库必须带 `--no-compress --no-prefilter`。**
这条已经用真机踩过一次：漏了 `--no-compress` 时 `bitmap_format=1`，而本工程
`LV_USE_FONT_COMPRESSED=0`，于是 `lv_font_get_bitmap_fmt_txt()` 对**每个字形**返回 NULL，
屏幕上**一个字都没有**，而且哪里都不报错（`LV_USE_LOG=0`，连告警都不打）。
把 `ui.cpp` 里的 `check_font()` 开机自检一起搬进 `main/`——40 行，防的正是这种静默故障。

**坑 6：flash 不是问题，但新的 JSON 类型会刷屏上位机。**

app 分区 4 MB，主工程当前 782 KB，模型 14 KB + 字库几 KB 可以忽略。

带宽也不是问题：控制台是内置 USB-Serial/JTAG（`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`），
走 USB CDC 而不是 115200 的 UART，所以 100 Hz 六轴 + 10 Hz 帧一起推跑得动
（`ear-context` 上实测 100.1 Hz、0 丢包）。

**真正的坑在上位机**：`tools/host/protocol.py` 的 `parse_line()` 对没见过的 `type`
返回 `ParseError(f"未知的 type={kind!r}")`，而 `state.py` 收到 error 就
`stats.bad_lines += 1`、`stats.unparsed_json += 1`、`note("无法解析的一行：…")`。
设备一旦按 100 Hz 推 `type:"imu"`，**面板每秒被刷 100 条解析错误**，而且
`bad_lines` / `unparsed_json` 这两个诊断计数立刻失去意义。

所以第一轮的第一步是**改上位机**：在 `parse_line()` 里认下 `imu` 这个类型
（直接忽略，或解析成一条独立统计）。**先上位机、后设备**——顺序反了会得到一个
"面板全是红字"的现场，而且很容易被误判成设备坏了。

---

## 四、分三轮，每轮都能单独编译 + 实测

### 第一轮：机械改动，零行为变化

1. **先改上位机**（见坑 7）——不然设备一开始推 `type:imu`，面板就被解析错误刷屏
2. `components/bmi270/` 挪到根，`ear-context` 改为 `EXTRA_COMPONENT_DIRS`
3. `imu.cpp` / `imu.hpp` 进 `main/`，加一个独立的 IMU 任务（core 0）
4. IMU 数据**只进串口 JSON**（多一个 `"type":"imu"` 行），不进任何决策、不进 UI
5. 把 `check_font()` 自检搬进 `demo_ui.cpp`

**验收**：六轴 100 Hz、`imu_drops=0`、上位机 `bad_lines`/`unparsed_json` **不增长**、
主 demo 的既有输出**逐字节不变**（同一段音频喂两边对比）。

### 第二轮：模型上机，纯显示 ✅ 已完成（2026-10-16）

1. `export_c.py` 生成到 `main/listening_model.h`
2. `window.hpp` 的 92 维窗口逻辑进 `main/`（**motion 两位保持 0**，见决策 2）
3. 在 `processing_task` 里维护窗口 → 推理 → 结果进 `EarbudCommand` 新字段 + UI 格子
4. **不加任何决策逻辑**——`AdaptController` 一行不改

实际做法与上面写的有三处不同，都是量出来才改的：

- 窗口逻辑落在 **`main/context_window.hpp`**，没有照搬 `ear-context` 的 `window.hpp`：
  后者在两处喂了与训练口径不符的输入（过时域闸门后的 `voice_likelihood`、以及「周一 00:00」
  的时间基线）。两处都已在**两个工程里**改成同一个函数，见 plan-status 第 29a/29b 项。
- **验收条件 2 换了实现方式，并且做到了。** "同一段音频分别喂两个固件、9 类逐帧一致"在声学上
  不可复现（两次播放不可能是同一段采样）。真正的等价性是"**同一个特征向量 → 同一个类别**"，
  它由三条证据钉住：导出头文件与 model-bench 那份逐字节相同、27/27 自检向量、92 维窗构造与
  `features.py` 逐维零差异（主机单测 23 条）。清单在 plan-status 第 29 项。
- 验收条件 1（推理 p95 上报）用 `latency` 报文的新字段实现，**没有**新增第 8 个延迟阶段：
  那张表的 7 个阶段是能量通路，名字同时被上位机的 `LOCAL_STAGES` 与两处测试引用，为一个
  推理耗时改它会把面板和协议层一起拖进来。

**验收结果**：p95 **161–180 µs**、**10.02 帧/秒、0 丢帧、0 解析错误**、`stack_high_water` **4,872 B**。
**"模型可用"不在这一轮的验收范围内**：真实房间里它仍把几乎所有声音判成 `other`，那是 v0.2
的语料短板（第 29 项），第三轮开工前必须先解决。

### 第三轮：接决策

1. `AdaptController` 接口扩展 + 单测
2. `horn` → `SAFETY_HOLD`
3. 在 `docs/product-alignment.md` 的决策表里补上"车声分类"这一行的状态

**验收**：`test/` 里的 `controller_test` 全绿；真机上鸣笛音频能进 `SAFETY_HOLD`，
且不误伤正常音乐和语音。

---

## 五、明确不做的

- **不把 `ear-context` 的 `app_main.cpp` 搬过来**。它是为实验裁剪的（没有 Mac 链路、
  没有 EQ、没有串口协议），搬过来等于把主 demo 拆掉。
- **不启用 `voiceprint_gate`**。它现在是"编译了、60 条单测、无调用方"，而声纹路线的
  止损门（`plan-status.md` 第 12 项：间距/抖动 ≥ 3 且 EER ≤ 5%）**还没跑**。
  在止损门有结果之前接进去，等于把一个未验证的假设变成产品行为。
- **不做定向人声放大**。已定：`IMU 头部朝向 ≠ 声源方位`，且本机是单声道
  （启动日志实锤：`slot_mode: MONO, slot_mask: 0x1`），没有空间信息。
