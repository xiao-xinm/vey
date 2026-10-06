"use strict";
const evaluations = {items: [], next: null, selected: null, offset: 0, status: "", request: 0, detailRequest: 0};
const replay = {payload: null, step: 0, request: 0};

function enableEvidenceNavigation() {
  for (const id of ["nav-tasks", "nav-evals", "nav-replay"]) $(id).disabled = false;
}
function switchEvidenceView(view) {
  if ($("workspace").hidden) return;
  for (const [name, id] of [["tasks", "task-view"], ["evals", "eval-view"], ["replay", "replay-view"]]) {
    $(id).hidden = name !== view;
    $("nav-" + name).classList.toggle("nav-active", name === view);
  }
  notify("");
  if (view === "evals" && !evaluations.items.length) loadEvaluations();
}
function clearReplay() {
  replay.request++; replay.payload = null; replay.step = 0;
  $("snapshot-file").value = "";
  $("replay-meta").replaceChildren();
  $("replay-controls").hidden = true;
  $("replay-detail").replaceChildren(node("p", "", "尚未载入快照。"));
}
function clearEvidence() {
  evaluations.request++; evaluations.detailRequest++;
  evaluations.items = []; evaluations.next = null; evaluations.selected = null;
  $("eval-runs").replaceChildren();
  $("eval-detail").replaceChildren(node("p", "", "选择一次实验查看样本。"));
  clearReplay();
  $("task-view").hidden = false; $("eval-view").hidden = true; $("replay-view").hidden = true;
  for (const name of ["tasks", "evals", "replay"]) $("nav-" + name).classList.toggle("nav-active", name === "tasks");
  for (const id of ["nav-tasks", "nav-evals", "nav-replay"]) $(id).disabled = true;
}
async function downloadTask(id, button) {
  button.disabled = true; notify("");
  try {
    const response = await fetch("/admin/api/tasks/" + id + "/export", {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) {
      if (response.status === 401) showLogin();
      throw new Error((await response.json()).message || "导出失败");
    }
    const url = URL.createObjectURL(await response.blob());
    const link = node("a"); link.href = url; link.download = "vey-audit-" + id + ".json";
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notify("快照已生成。分享前请检查脱敏结果；可在离线复核中逐步查看。");
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; }
}
function known(value) { return value === null || value === undefined ? "未知" : String(value); }
function modeLabel(mode) { return ({live: "真实模型", scripted: "预设回放", rules: "规则"})[mode] || mode || "未知模式"; }
function scopeLabel(scope) { return scope === "full_readonly_diagnosis" ? "完整只读诊断" : "路由与单步规划"; }
function renderEvaluationRuns() {
  const rows = evaluations.items.map(run => {
    const row = node("button", "task" + (run.run_id === evaluations.selected ? " selected" : ""));
    const top = node("div", "task-top");
    top.append(node("strong", "task-title", run.dataset_version || "未记录数据版本"), node("span", "badge", modeLabel(run.mode)));
    const m = run.metrics;
    row.append(top, node("p", "task-meta", date(run.started_at || run.archived_at)),
      node("p", "task-summary", `${scopeLabel(run.scope)} · ${run.strategy || "诊断循环"} · 通过 ${known(m.passed)}/${known(m.executed)}`));
    row.addEventListener("click", () => { evaluations.selected = run.run_id; evaluations.offset = 0; evaluations.status = ""; renderEvaluationRuns(); loadEvaluationDetail(); });
    return row;
  });
  $("eval-runs").replaceChildren(...(rows.length ? rows : [node("p", "empty", "暂无归档实验")]));
  $("eval-more").hidden = !evaluations.next;
}
async function loadEvaluations(append = false) {
  const version = ++evaluations.request;
  $("eval-refresh").disabled = true; $("eval-more").disabled = true;
  try {
    const page = await api("evaluations" + (append && evaluations.next ? "?before=" + evaluations.next : ""));
    if (version !== evaluations.request) return;
    evaluations.items = append ? [...evaluations.items, ...page.items] : page.items;
    evaluations.next = page.next; renderEvaluationRuns();
  } catch (error) {
    if (version === evaluations.request) $("eval-runs").replaceChildren(node("p", "empty", "评测库暂不可读，任务查询仍可使用。"));
    notify(error.message);
  } finally { $("eval-refresh").disabled = false; $("eval-more").disabled = false; }
}
async function loadEvaluationDetail() {
  const version = ++evaluations.detailRequest;
  const query = new URLSearchParams({offset: evaluations.offset});
  if (evaluations.status) query.set("status", evaluations.status);
  $("eval-detail").replaceChildren(node("p", "", "正在读取评测样本…"));
  try {
    const result = await api("evaluations/" + evaluations.selected + "?" + query);
    if (version === evaluations.detailRequest) renderEvaluationDetail(result);
  } catch (error) { notify(error.message); }
}
function renderEvaluationDetail({run, samples, prompt_versions, next_offset}) {
  const root = $("eval-detail"), m = run.metrics;
  root.replaceChildren(node("h2", "", scopeLabel(run.scope)), node("div", "detail-id", run.run_id));
  const stats = node("div", "metric-row");
  stats.append(metric("通过 / 已运行", known(m.passed) + " / " + known(m.executed)), metric("未运行", known(m.not_run)),
    metric("模型请求", known(m.model_calls)), metric("总 tokens", known(m.tokens?.total_tokens)),
    metric("估算费用", m.estimated_cost == null ? "未知" : known(m.estimated_cost)));
  root.append(stats, node("p", "boundary-note", run.provenance_note));
  const versionText = [
    `数据：${run.dataset_version || "未知"} · ${run.split || "未声明分组"}`,
    `模式：${modeLabel(run.mode)} · 运行状态：${run.status}`,
    `提示：${run.prompt_version || prompt_versions.filter(Boolean).join(" / ") || "未记录"}`,
    `循环：${run.loop_version || "不适用或未记录"}`,
    `评分：${run.scoring_label}`,
    `P50 / P95：${known(m.latency_ms_p50)} / ${known(m.latency_ms_p95)} ms`,
    "未标价的费用不可当作 0；未知评分版本的历史报告不作分数对比。"
  ];
  root.append(node("div", "result", versionText.join("\n")));
  const provenance = node("details");
  provenance.append(node("summary", "", "查看数据、实现和报告哈希及计价依据"),
    node("pre", "", JSON.stringify({dataset_hash: run.dataset_hash, implementation_hash: run.implementation_hash,
      report_hash: run.report_hash, pricing: run.pricing, metrics: m}, null, 2)));
  root.append(provenance, node("h3", "", "逐项样本与失败记录"));
  const filter = node("select"); filter.setAttribute("aria-label", "样本状态");
  for (const [v, label] of [["", "全部样本"], ["failed", "仅失败"], ["passed", "仅通过"], ["not_run", "未运行"], ["uncovered", "未覆盖"]]) {
    const option = node("option", "", label); option.value = v; filter.append(option);
  }
  filter.value = evaluations.status;
  filter.addEventListener("change", () => { evaluations.status = filter.value; evaluations.offset = 0; loadEvaluationDetail(); });
  root.append(filter);
  if (!samples.length) root.append(node("p", "", "没有符合筛选的样本。"));
  for (const sample of samples) {
    const item = node("details", "sample");
    const outcome = ({passed: "通过", failed: "失败", not_run: "未运行", uncovered: "未覆盖"})[sample.status] || sample.status;
    item.append(node("summary", "", `${sample.case_id} · 第 ${sample.repetition} 次 · ${outcome}`));
    if (sample.result.failures?.length) item.append(node("p", "failure-note", sample.result.failures.join(" / ")));
    if (sample.truncated) item.append(node("p", "failure-note", "超过 200 KB，未加载完整样本。"));
    item.append(node("pre", "", JSON.stringify(sample.result, null, 2))); root.append(item);
  }
  const pager = node("div", "replay-controls");
  const prev = node("button", "secondary", "← 上一页"), next = node("button", "secondary", "下一页 →");
  prev.disabled = evaluations.offset === 0; next.disabled = next_offset === null;
  prev.addEventListener("click", () => { evaluations.offset = Math.max(0, evaluations.offset - 10); loadEvaluationDetail(); });
  next.addEventListener("click", () => { evaluations.offset = next_offset; loadEvaluationDetail(); });
  pager.append(prev, node("span", "muted", `第 ${evaluations.offset + 1} 条起`), next); root.append(pager);
}
async function loadLocalSnapshot(file) {
  clearReplay(); if (!file) return;
  const version = replay.request;
  try {
    if (file.size > 1000000) throw new Error("文件超过 1 MB，未读取。");
    const envelope = JSON.parse(await file.text());
    if (!envelope || typeof envelope !== "object" || Object.keys(envelope).sort().join(",") !== "format,payload,sha256" || envelope.format !== "vey-audit-v1" || typeof envelope.payload !== "string") throw new Error("不支持的快照格式");
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(envelope.payload)))).map(b => b.toString(16).padStart(2, "0")).join("");
    if (hash !== envelope.sha256) throw new Error("文件校验不一致，请重新导出。");
    const p = JSON.parse(envelope.payload);
    if (!p || p.format !== "vey-audit-v1" || !p.task || !/^[a-f0-9]{32}$/.test(p.task.id) || !Array.isArray(p.events) || p.events.length > 100 || typeof p.events_truncated !== "boolean") throw new Error("快照结构无效");
    let last = -1;
    for (const e of p.events) {
      if (!e || !Number.isSafeInteger(e.id) || e.id <= last || !Object.hasOwn(kindLabels, e.kind) || !e.data || Array.isArray(e.data) || typeof e.data !== "object") throw new Error("审计事件格式或顺序无效");
      last = e.id;
    }
    if (version !== replay.request) return;
    replay.payload = p; replay.step = 0;
    $("replay-meta").replaceChildren(node("p", "", `已读取 ${p.events.length} 条事件 · 校验一致 · ${p.events_truncated ? "记录已截断" : "导出范围内未截断"}`),
      node("p", "footnote", "文件不会上传，校验和不证明来源真实性。分享前仍需人工检查敏感信息。"));
    $("replay-controls").hidden = false; renderReplayStep();
  } catch (error) { if (version === replay.request) notify(error.message); }
}
function renderReplayStep() {
  const p = replay.payload; if (!p) return;
  const root = $("replay-detail"); root.replaceChildren();
  $("replay-position").textContent = replay.step === 0 ? "任务摘要" : `${replay.step} / ${p.events.length}`;
  $("replay-prev").disabled = replay.step === 0; $("replay-next").disabled = replay.step === p.events.length;
  if (replay.step === 0) {
    root.append(node("h2", "", "任务摘要 · 仅记录展示"), node("pre", "", JSON.stringify(p.task, null, 2)));
    if (p.operation) root.append(node("h3", "", "操作历史（不执行）"), node("pre", "", JSON.stringify(p.operation, null, 2)));
  } else {
    const e = p.events[replay.step - 1];
    root.append(node("h2", "", kindLabels[e.kind]), node("p", "muted", date(e.created_at)), node("pre", "", JSON.stringify(e.data, null, 2)));
    const reads = p.events.slice(0, replay.step).filter(x => x.kind === "tool").length;
    root.append(node("p", "footnote", `到此已展示 ${reads} 条读取工具记录。此计数不是诊断证据 E 编号，也不包含未落库的失败尝试。`));
  }
}
$("nav-tasks").addEventListener("click", () => switchEvidenceView("tasks"));
$("nav-evals").addEventListener("click", () => switchEvidenceView("evals"));
$("nav-replay").addEventListener("click", () => switchEvidenceView("replay"));
$("eval-refresh").addEventListener("click", () => loadEvaluations());
$("eval-more").addEventListener("click", () => loadEvaluations(true));
$("snapshot-file").addEventListener("change", event => loadLocalSnapshot(event.target.files[0]));
$("replay-next").addEventListener("click", () => { if (replay.payload && replay.step < replay.payload.events.length) replay.step++; renderReplayStep(); });
$("replay-prev").addEventListener("click", () => { if (replay.step > 0) replay.step--; renderReplayStep(); });
$("replay-clear").addEventListener("click", clearReplay);
