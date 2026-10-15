// 用 Node 跑一遍面板的渲染函数，验证"喂进真实快照，界面上会出现什么"。
//
// 为什么值得这么做：这台机器上无头浏览器拿不到显示器（CVDisplayLinkCreateWithCGDisplay
// failed），系统也不允许截屏，所以"打开看一眼"这条路是断的。但界面里唯一会算错的地方
// 是 render() 这一层——它把设备数字搬到 DOM 上。把 DOM 换成桩、喂进活动主机的快照，
// 就能断言它到底写了什么，而不是"看起来应该没问题"。
//
// 用法：node tools/host/tests/render_check.js <snapshot.json>
// 退出码：0 全部通过 · 1 有断言失败

const fs = require("fs");
const path = require("path");

const htmlPath = path.join(__dirname, "..", "web", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

// ---------------------------------------------------------------- DOM 桩
// 只接受 HTML 里真实存在的 id。旧版本对任何 id 都 lazy 创建一个元素，于是"改了 id
// 忘了同步脚本"这种最常见的界面故障，在这个脚本里反而是绿的——桩替被测代码把错误补上了。
// 现在引用不存在的 id 会被记下来，并在末尾作为失败报出。
const REAL_IDS = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
const unknownIds = new Set();
const elements = new Map();
function makeElement(id) {
  return {
    id,
    textContent: "",
    innerHTML: "",
    className: "",
    style: {},
    value: "10",
    disabled: false,
    addEventListener() {},
    getContext() {
      // 面板用了 canvas 画趋势图与 24 频段调音，所以要给全 2D 上下文的方法与属性。
      // 漏一个方法就会变成"渲染校验自己抛异常"，看起来像界面坏了。
      return {
        setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
        stroke() {}, fillText() {}, fill() {}, fillRect() {}, strokeRect() {},
        save() {}, restore() {}, closePath() {}, arc() {},
        measureText() { return { width: 0 }; },
        strokeStyle: "", lineWidth: 1, fillStyle: "", font: "",
      };
    },
    clientWidth: 900,
    width: 900,
    height: 150,
  };
}
const document = {
  getElementById(id) {
    if (!REAL_IDS.has(id)) {
      unknownIds.add(id);
      return null;
    }
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  },
};
const window = { devicePixelRatio: 1, addEventListener() {} };
class EventSource {
  constructor() { this.onmessage = null; this.onerror = null; }
}
function fetch() {
  return Promise.resolve({ json: () => Promise.resolve({ ok: true, command: "rec start 10" }) });
}

// ---------------------------------------------------------------- 加载面板脚本
// 面板脚本最后会调用 connect() 建 SSE 连接；这里 EventSource 是桩，不会真连。
const factory = new Function("document", "window", "EventSource", "fetch", "console", script);
factory(document, window, EventSource, fetch, console);

const render = window.__render;
if (typeof render !== "function") {
  console.error("面板脚本没有暴露 window.__render，无法验证渲染");
  process.exit(1);
}

// ---------------------------------------------------------------- 断言
let passed = 0;
const failed = [];
function check(condition, label) {
  if (condition) passed += 1;
  else failed.push(label);
}
function text(id) {
  // 面板会把数字直接赋给 textContent（浏览器会自动转成字符串），桩不会，
  // 所以这里统一转字符串，断言才能用 includes。
  const element = elements.get(id);
  if (!element) return "";
  const value = element.textContent;
  return value === undefined || value === null ? "" : String(value);
}
function htmlOf(id) {
  const element = elements.get(id);
  return element ? element.innerHTML : "";
}

const snapshotPath = process.argv[2];
if (!snapshotPath) {
  console.error("用法：node render_check.js <snapshot.json>");
  process.exit(2);
}
const snapshot = JSON.parse(fs.readFileSync(snapshotPath, "utf8"));

elements.clear();
// render() 自己抛异常也是一种失败（例如某个 id 在 HTML 里不存在，$() 返回 null）。
// 不能让它带着堆栈崩掉——那样退出码虽然也是非零，但看不出"是哪条契约被破坏"。
try {
  render(snapshot);
} catch (error) {
  console.log(`渲染检查：0 条通过，1 条失败`);
  console.log(`  ✗ render() 抛异常：${error && error.message ? error.message : error}`);
  if (unknownIds.size) {
    console.log(`  ✗ 脚本引用了 HTML 里不存在的 id：${[...unknownIds].join(", ")}`);
  }
  process.exit(1);
}

const frame = snapshot.frame;
check(text("source-text").includes(snapshot.source_text), "顶部显示数据源");
check(text("frames").includes(String(snapshot.stats.commands)), "顶部显示控制帧数");
check(text("age") !== "", "顶部显示数据年龄");

if (frame) {
  check(text("env") === frame.environment_text, `环境显示为 ${frame.environment_text}`);
  check(text("state") === frame.state_text, `动作显示为 ${frame.state_text}`);
  // 「现实人声增益意图」这一格显示的是 external_voice_gain_db。旧断言比的是
  // speech_gain_db——提交 eb53704 把这一格换了语义之后它就成了假断言：真机上两者恰好都是
  // 0.0 时它会"通过"，一有增益就报失败。比对一个字段就要比对照界面的那一个。
  check(text("gain").includes(frame.external_voice_gain_db.toFixed(2)), "现实人声增益按两位小数显示");
  // 口径：本产品只有增益与调音，没有降噪。快照里不该有这个字段，界面上不该有这一格。
  check(frame.noise_reduction_db === undefined, "快照不得下发降噪量");
  check((elements.get("nr") || { textContent: "x" }).textContent === "x", "界面不得再有降噪格子");
  // 同一格的第二次语义漂移：id 还叫 media，内容已经是「决策模式」= 设备上报的 mode。
  check(text("media") === (frame.mode || "LEGACY"), "决策模式显示设备上报的 mode");
  check(text("link") === frame.link_text, "控制链路用中文口径");
  check(text("link-raw").includes(frame.link), "同时显示设备原始字段");
  check(text("seq").includes(String(frame.sequence)), "显示帧序号");
  if (frame.link_reported_connected === false) {
    check(htmlOf("alerts").includes("模拟"), "设备自称 simulated 时必须明确提示");
  }
}

// 中继延迟：这条通路上原来最缺的一块，也是"改了参数延迟只动一点点"的答案。
// 三种情形必须显示成三件不同的事：有实测数字 / 播放端不报告 / 没开中继。
if (snapshot.relay === null || snapshot.relay === undefined) {
  check(text("relay-total") === "未开启", "没开中继时显示未开启，而不是 0 ms");
} else if (!snapshot.relay.measurable) {
  check(text("relay-total") === "测不到", "播放端不报告时必须显示测不到，不许显示成 0");
  check(text("relay-note").includes("不等于延迟为零"), "没有数字必须解释成没有数字");
} else {
  const relay = snapshot.relay;
  check(text("relay-total").includes(String(Math.round(relay.est_out_ms))),
        "中继卡片显示实测合计延迟");
  check(text("relay-detail").includes(String(Math.round(relay.driver_ms))),
        "明细里拆出驱动侧输出延迟（蓝牙那一段）");
  check(text("relay-detail").includes(relay.device || ""),
        "写明声音实际去了哪个设备");
  check(text("relay-note").includes("不含设备侧打包"), "必须写明这个数不含哪一段");
  if (relay.pinned === false) {
    check(text("relay-note").includes("没有钉住"), "没钉住目标设备时必须报警");
  }
}

// 另外两种情形只在合成快照上出现，而它们最容易说错：把"没开中继"或"播放端不报告"
// 显示成 0 ms，等于用一个漂亮的数字掩盖"这条链路没有测量"。
if (snapshot.relay) {
  render(Object.assign({}, snapshot, { relay: null }));
  check(text("relay-total") === "未开启", "未开启中继时显示未开启，不是 0 ms");
  render(Object.assign({}, snapshot, {
    relay: { measurable: false, note: "播放端不报告排队深度（ffplay 没有播放位置接口）" },
  }));
  check(text("relay-total") === "测不到", "播放端不报告时显示测不到，不是 0 ms");
  render(snapshot); // 还原：后面的断言还在这份快照上
}

// Mac 播放与执行端：面板必须说清"决策已产生"与"Mac 已执行"是两件事，
// 而且第三件（AirPods 是否已播放）本链路无法验证，不得写成已验证。
if (snapshot.player) {
  const player = snapshot.player;
  check(text("track").length > 0, "播放卡片显示曲目");
  check(["▶ 播放", "⏸ 暂停"].includes(text("btn-play")), "播放按钮按播放状态切换文案");
  const disabled = (elements.get("btn-play") || {}).disabled;
  check(disabled === (player.available === false), "播放器不可用时按钮必须禁用");
  const receipt = text("receipt");
  check(receipt.includes(`设备自报 execution=${frame ? frame.execution : "unknown"}`),
        "回执写明设备自报的 execution");
  check(receipt.includes("AirPods 是否已播放：无法验证"),
        "回执不得声称 AirPods 已播放（本链路没有回执通路）");
  if (player.available) {
    check(text("apply-note").includes("Mac 真正设进 EQ"),
          "必须有说明告诉读者实心柱是 Mac 真正应用的值");
    check(text("apply-note").includes("深度"),
          "面板必须写明放大倍率是本机演示参数，不是设备的建议");
  }
  if (player.execution === "applied") {
    check(receipt.includes("已执行"), "回执为 applied 时必须写明已执行");
  }
  if (player.execution === "idle") {
    check(receipt.includes("未执行"), "没在播放时必须写明决策没有被执行");
  }
  // 进度条：位置时刻要显示，滑块要跟着 position/duration 走。用户明确要过这个控件。
  check(text("track-pos").includes(":"), "进度条旁边显示 m:ss 时刻");
  const seek = elements.get("player-seek");
  check(seek !== undefined, "进度条存在且被渲染到");
  if (seek && player.duration_s) {
    const expected = Math.round(((player.position_s || 0) / player.duration_s) * 1000);
    check(Math.abs(Number(seek.value) - expected) <= 2,
          `进度条位置与 position/duration 一致（期望 ${expected}，实得 ${seek.value}）`);
  }
} else {
  console.log("  （提示：这份快照没有 player 段，已跳过播放与回执断言）");
}

// 延迟表：逐行核对"零"的解释。两种零必须分开——本地阶段为 0 是未实现（声纹推理），
// 对端阶段 n=0 是没有样本（对端未接通）。旧版按 n===0 判断，正好把两者标反，
// 而且只断言"出现过未实现"，等于给错误标注背书。
if (snapshot.latency) {
  const body = htmlOf("latency-body");
  check(body.includes("capture") && body.includes("analyse"), "延迟表列出了各阶段");
  for (const stage of snapshot.latency.stages) {
    // 每行形如 <tr><td>名字<span…>note</span></td>…
    const rowMatch = body.match(new RegExp(`<tr><td>${stage.stage}([\\s\\S]*?)</tr>`));
    check(rowMatch !== null, `延迟表里有 ${stage.stage} 行`);
    if (!rowMatch) continue;
    const row = rowMatch[0];
    const isLocal = stage.local !== undefined ? stage.local : ["capture", "analyse", "embed", "decide"].includes(stage.stage);
    if (isLocal && stage.p50_ms === 0 && stage.n > 0) {
      check(row.includes("未实现"), `${stage.stage} 本地阶段值为 0 → 必须标"未实现"`);
    }
    if (!isLocal && stage.n === 0) {
      check(row.includes("无样本"), `${stage.stage} 对端阶段无样本 → 必须标"无样本"，不能说未实现`);
      check(!row.includes("未实现"), `${stage.stage} 不得被标成"未实现"`);
    }
    if (isLocal && stage.n > 0 && stage.p50_ms > 0) {
      check(!row.includes("未实现") && !row.includes("无样本"), `${stage.stage} 有实测值时不得加任何"缺失"标注`);
    }
  }
  check(
    text("latency-note").includes("打点自身开销"),
    "延迟出处写明打点开销，避免把打点成本当成被测对象"
  );
} else {
  console.log("  （提示：这份快照没有 latency 汇总，已跳过延迟表断言）");
}

// 标签必须写明"主机侧到达间隔"，并且要有一句说明 min≈0 是批量到达伪影。
// 这条断言挡的是"把主机侧间隔误当成设备节拍"那种最容易被引用的错。
const statsHtml = htmlOf("stats-body");
check(statsHtml.includes("主机侧到达间隔"), "统计里标明是主机侧到达间隔，不是设备节拍");
check(statsHtml.includes("批量到达") || statsHtml.includes("多行"), "统计里解释 min≈0 的来源");
check(htmlOf("problems").length > 0, "异常列表已渲染");
check(htmlOf("events").length > 0, "事件列表已渲染");
// 原始输出区走的是 textContent（不是 innerHTML），没有非协议行时显示占位符——那是
// **正确行为**，不是没渲染。读错属性会让这条断言假失败。
const raw = text("raw");
check(raw.length > 0, "原始输出区已渲染");
check(raw === "—" || raw.includes("{") || raw.includes("I ("),
      `原始输出内容是行或占位符（实得 ${JSON.stringify(raw.slice(0, 40))}）`);
check(text("capture-text") === snapshot.capture.text, "录音状态文案");

// 丢帧必须在界面上留下可见痕迹：告警文字 + 趋势图上的竖线逻辑（后者由代码审查覆盖）。
// 回控降级必须看得见：发了命令却零应答时，页面上要有明确告警。
if (snapshot.control && snapshot.control.unresponsive) {
  check(htmlOf("alerts").includes("一次都没有得到回应"), "零应答时必须报回控降级");
  check(
    (elements.get("control-warn") || { style: {} }).style.display === "block",
    "回控降级时那块提示必须显示出来",
  );
} else {
  console.log("  （提示：这份快照没有零应答的回控，已跳过回控降级断言）");
}

if (snapshot.stats.gaps > 0) {
  check(htmlOf("alerts").includes("跳变"), "丢帧时告警区必须写明序号跳变");
} else {
  // 静默跳过条件块会让"没测到"看起来像"测过了"，所以显式说出来。
  console.log("  （提示：这份快照 gaps=0，已跳过丢帧告警断言——要覆盖它请用注入丢帧的快照）");
}

// 引用不存在的 id 必须作为失败报出（见 REAL_IDS 的注释）。
check(unknownIds.size === 0, `面板脚本不得引用不存在的元素 id（实得 ${[...unknownIds].join(", ") || "无"}）`);

// 调音那一块的文案必须说清数字从哪来：设备报了 eq_gain_db 就是实测，
// 没报才退回推导——把推导值画得像实测值，是这块面板最容易犯的错。
if (snapshot.frame) {
  const note = text("eq-note");
  if (snapshot.frame.tuning) {
    check(note.includes("设备实测"), "有 eq_gain_db 时必须标明是设备实测调音");
    check(
      note.includes(String(snapshot.frame.tuning.lifted_bands)),
      "调音文案要给出抬起的频段数",
    );
    check(
      snapshot.frame.tuning.band_gain_db.length === 24,
      "调音数组应当是 24 个频段",
    );
  } else {
    check(note.includes("推导"), "没有 eq_gain_db 时必须标明图形是推导的");
  }
}

console.log(`渲染检查：${passed} 条通过，${failed.length} 条失败`);
if (failed.length) {
  failed.forEach((item) => console.log(`  ✗ ${item}`));
  process.exit(1);
}
// 打印关键格子，便于人工核对"界面上到底会显示什么"
console.log("  环境:", text("env"), "| 动作:", text("state"),
            "| 语音段增益（调音幅度）:", text("gain"),
            "| 媒体:", text("media"), "| 链路:", text("link"));
console.log("  数据年龄:", text("age"), "| 帧数:", text("frames"), "| 丢帧:", text("lost"));
process.exit(0);
