const assert = require("assert");
const fs = require("fs");
const path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const elements = new Map();
const document = {getElementById(id) {
  assert(ids.has(id), `missing DOM element: ${id}`);
  if (!elements.has(id)) elements.set(id, {textContent:"", style:{}, addEventListener(){}});
  return elements.get(id);
}};
const window = {addEventListener(){}};
class EventSource {}
new Function("document", "window", "EventSource", "fetch", "console", script)(
  document, window, EventSource, () => Promise.resolve({}), console);
const render = window.__renderSources;
const text = id => elements.get(id).textContent;
const channels = {};
for (const name of ["traffic", "horn_alarm", "speech", "wind", "music", "keyboard", "mechanical"])
  channels[name] = {score:0.9, detected:["traffic", "speech", "wind"].includes(name), validated:false};
channels.mechanical = {score:null, detected:null, validated:false};
const sources = {valid:true, status:"ready", version:"synthetic-test", channels};
const snapshot = {connected:true, control_data_live:true, frame:{sound_sources:sources}};
render(snapshot);
assert.strictEqual(text("sources"), "车流＋人声＋风噪");
assert(text("sources-detail").includes("未验证"));
assert(text("sources-detail").includes("未训练"));
render({...snapshot, control_data_live:false});
assert.strictEqual(text("sources"), "数据中断");
assert.strictEqual(text("sources-detail"), "");
for (const channel of Object.values(channels)) channel.detected = false;
render(snapshot);
assert.strictEqual(text("sources"), "未检出已支持声源");
assert(!text("sources").includes("安静"));
render({...snapshot, frame:{sound_sources:{valid:false, status:"model_unavailable"}}});
assert.strictEqual(text("sources"), "模型待训练");
render({...snapshot, frame:{}});
assert.strictEqual(text("sources"), "当前固件未提供");
console.log("sound sources rendering: PASS (synthetic states)");
