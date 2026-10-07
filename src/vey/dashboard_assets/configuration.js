"use strict";
const configurationState = {request: 0, current: null, proof: null, change: null, rollback: null};
function clearConfiguration() {
  configurationState.request++; configurationState.current = null;
  configurationState.rollback = null; invalidateConfiguration();
  $("config-rows").replaceChildren(); $("config-history").replaceChildren();
  $("config-version").textContent = ""; $("config-reason").value = "";
}
function invalidateConfiguration() {
  configurationState.proof = null; configurationState.change = null;
  $("config-publish").disabled = true; $("config-confirm").checked = false;
  $("config-diff").replaceChildren();
}
function editedConfiguration() { configurationState.request++; configurationState.rollback = null; invalidateConfiguration(); }
function configurationRows() {
  return [...$("config-rows").children].map(row => ({
    key: row.querySelector(".config-key").value.trim(),
    aliases: row.querySelector(".config-aliases").value.split(/[,，\n]/).map(x => x.trim()).filter(Boolean),
    protected: row.querySelector(".config-protected").checked,
    health_url: row.querySelector(".config-health").value.trim() || null
  }));
}
function renderConfigurationRows(services) {
  const baseline = configurationState.current;
  $("config-rows").replaceChildren(...services.map((service, index) => {
    const row = node("fieldset", "config-row"); row.append(node("legend", "", `服务 ${index + 1}`));
    const locked = baseline.locked_services.includes(service.key);
    for (const [label, cls, value] of [["Compose 项目/服务", "config-key", service.key], ["别名（逗号分隔）", "config-aliases", service.aliases.join("，")], ["健康检查地址（可选）", "config-health", service.health_url || ""]]) {
      const field = node("label", "config-field", label), input = node("input", cls);
      input.value = value; input.maxLength = cls === "config-health" ? 512 : cls === "config-key" ? 120 : 968;
      input.disabled = locked && cls === "config-key";
      input.addEventListener("input", editedConfiguration); field.append(input); row.append(field);
    }
    const protectedLabel = node("label", "config-toggle", locked ? "核心保护（不可解除）" : "禁止启停和重启");
    const protectedInput = node("input", "config-protected"); protectedInput.type = "checkbox";
    protectedInput.checked = service.protected || baseline.protected_projects.includes(service.key.split("/")[0]);
    protectedInput.disabled = locked || baseline.protected_projects.includes(service.key.split("/")[0]);
    protectedInput.addEventListener("change", editedConfiguration); protectedLabel.prepend(protectedInput); row.append(protectedLabel);
    const remove = node("button", "quiet", "移除登记"); remove.type = "button"; remove.disabled = locked;
    remove.addEventListener("click", () => { const values = configurationRows(); values.splice(index, 1); editedConfiguration(); renderConfigurationRows(values); });
    row.append(remove); return row;
  }));
}
async function loadConfiguration() {
  const version = ++configurationState.request; invalidateConfiguration();
  $("config-version").textContent = "正在读取配置…";
  try {
    const data = await api("configuration"); if (version !== configurationState.request) return;
    configurationState.current = data; configurationState.rollback = null;
    $("config-version").textContent = "生效版本：" + data.revision;
    $("config-reason").value = ""; renderConfigurationRows(data.services);
    $("config-history").replaceChildren(...data.history.map(entry => {
      const item = node("div", "result");
      item.append(node("p", "", `${date(entry.created_at)} · ${entry.id === data.revision ? "当前版本" : "历史版本"}`), node("p", "detail-id", entry.id), node("p", "", entry.reason));
      const rollback = node("button", "secondary", "载入此版本作为回滚草稿"); rollback.disabled = entry.id === data.revision;
      rollback.addEventListener("click", () => {
        configurationState.request++; invalidateConfiguration(); configurationState.rollback = entry.id;
        renderConfigurationRows(entry.services); $("config-reason").value = "回滚至 " + entry.id;
        notify("已载入回滚草稿，尚未发布。请先校验差异，再确认发布。");
      }); item.append(rollback); return item;
    }));
  } catch (error) { if (version === configurationState.request) { configurationState.current = null; $("config-version").textContent = "配置暂不可读取"; notify(error.message); } }
}
async function previewConfiguration() {
  if (!configurationState.current) return;
  const version = ++configurationState.request;
  const change = {base_revision: configurationState.current.revision, services: configurationRows(), reason: $("config-reason").value, rollback_of: configurationState.rollback};
  invalidateConfiguration();
  try {
    const result = await api("configuration/preview", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(change)});
    if (version !== configurationState.request) return;
    configurationState.change = change; configurationState.proof = result.proof;
    const root = $("config-diff"); root.append(node("p", "boundary-note", result.effect), node("p", "", "校验有效期：" + date(result.expires_at * 1000)));
    for (const warning of result.warnings) root.append(node("p", "boundary-note", warning));
    for (const diff of result.diff) {
      root.append(node("h3", "", diff.key), node("pre", "", JSON.stringify({发布前: diff.before, 发布后: diff.after}, null, 2)));
    }
    notify("校验通过，请核对差异并勾选确认。");
  } catch (error) { if (version === configurationState.request) notify(error.message); }
}
async function publishConfiguration() {
  if (!configurationState.proof || !$("config-confirm").checked) return;
  const payload = {...configurationState.change, proof: configurationState.proof};
  const version = ++configurationState.request; invalidateConfiguration();
  try {
    const result = await api("configuration/publish", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    if (version !== configurationState.request) return;
    await loadConfiguration(); notify(`已发布新版本，作废 ${result.cancelled_confirmations} 条旧确认。`);
  } catch (error) { if (version === configurationState.request) notify(error.message + "。请刷新核对生效版本。"); }
}
$("nav-config").addEventListener("click", () => switchEvidenceView("config"));
$("config-refresh").addEventListener("click", loadConfiguration);
$("config-preview").addEventListener("click", previewConfiguration);
$("config-publish").addEventListener("click", publishConfiguration);
$("config-confirm").addEventListener("change", () => { $("config-publish").disabled = !configurationState.proof || !$("config-confirm").checked; });
$("config-reason").addEventListener("input", editedConfiguration);
$("config-add").addEventListener("click", () => {
  if (!configurationState.current) return;
  const rows = configurationRows(); if (rows.length >= 50) { notify("最多登记 50 个服务"); return; }
  rows.push({key: "", aliases: [], protected: true, health_url: null}); editedConfiguration(); renderConfigurationRows(rows);
});
