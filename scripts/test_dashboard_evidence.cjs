// Pure JavaScript contract checks with an inert DOM substitute; not a browser layout test.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {webcrypto, createHash} = require("node:crypto");
function element() {
  return {hidden: false, disabled: false, value: "", textContent: "", children: [],
    addEventListener() {}, setAttribute() {}, classList: {toggle() {}},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; }};
}
const nodes = new Map();
const get = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
let notice = "", requests = 0;
const context = vm.createContext({
  $: get, node: (tag, cls, text) => Object.assign(element(), {tag, textContent: text || ""}),
  notify: message => { notice = message; },
  date: value => value, metric: (label, value) => ({label, value}),
  kindLabels: {intent: "识别请求", tool: "工具", operation: "操作结果", error: "错误"},
  crypto: webcrypto, TextEncoder, URLSearchParams,
  fetch() { requests++; throw Error("Network is forbidden during offline review"); },
  api() { requests++; throw Error("API is forbidden during offline review"); }
});
vm.runInContext(fs.readFileSync("src/vey/dashboard_assets/evidence.js", "utf8"), context);
function snapshot(events) {
  const payload = JSON.stringify({format: "vey-audit-v1", task: {id: "a".repeat(32)}, events,
    operation: {action: "restart", status: "succeeded"}, events_truncated: true});
  return JSON.stringify({format: "vey-audit-v1", payload, sha256: createHash("sha256").update(payload).digest("hex")});
}
async function load(raw) {
  context.input = {size: Buffer.byteLength(raw), text: async () => raw};
  await vm.runInContext("loadLocalSnapshot(input)", context);
}
(async () => {
  const raw = snapshot([{id: 1, kind: "operation", data: {action: "restart"}},
    {id: 2, kind: "tool", data: {log_excerpt: "<img src=x onerror=alert(1)>"}}]);
  await load(raw);
  assert.equal(get("replay-controls").hidden, false);
  vm.runInContext("replay.step = 1; renderReplayStep()", context);
  assert.equal(get("replay-position").textContent, "1 / 2");
  assert.equal(requests, 0);
  await load(raw.replace("restart", "stop"));
  assert.match(notice, /校验不一致/);
  assert.equal(get("replay-controls").hidden, true);
  await load(snapshot([{id: 2, kind: "tool", data: {}}, {id: 1, kind: "tool", data: {}}]));
  assert.match(notice, /顺序无效/);
  await load(snapshot([{id: 1, kind: "shell", data: {command: "anything"}}]));
  assert.match(notice, /格式或顺序无效/);
  await load("a".repeat(1000001));
  assert.match(notice, /超过 1 MB/);
  await load(raw);
  vm.runInContext("clearEvidence()", context);
  assert.equal(get("replay-controls").hidden, true);
  assert.equal(get("nav-replay").disabled, true);
  assert.equal(requests, 0);
  console.log("JavaScript contracts passed: offline stepping, checksum/order/size rejection, logout clearing, zero network calls");
  let release;
  context.api = () => new Promise(resolve => { release = resolve; });
  const pending = vm.runInContext("loadOperations()", context);
  vm.runInContext("clearEvidence()", context);
  release({configured: false});
  await pending;
  assert.equal(get("ops-report").children.length, 0);
  assert.equal(get("ops-view").hidden, true);
  assert.equal(get("nav-ops").disabled, true);
  context.api = async () => ({configured: true, latest: {state: "invalid"}, last_success: {state: "missing"}});
  await vm.runInContext("loadOperations()", context);
  assert.ok(get("ops-report").children.some(x => /不能判定备份成功/.test(x.textContent)));
  console.log("Operations contracts passed: invalid report warning and logout discards in-flight response");
})().catch(error => { console.error(error); process.exitCode = 1; });
