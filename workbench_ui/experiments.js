"use strict";

const experimentApi = "/api/experiments";
const experimentStatuses = {
  planned: "已冻结", running: "已登记开始", succeeded: "已登记成功",
  failed: "已登记失败", cancelled: "已登记取消",
};
const conditionStatuses = {
  conditions_match: "当前条件与基线一致", conditions_changed: "当前条件与基线有差异",
  check_incomplete: "部分条件无法检查",
};
const conditionKinds = {code: "源码", inputs: "输入", environment: "环境", git: "Git"};
const experimentModes = {agent: "任务", code: "代码工程", research: "科研构思", chat: "Chat"};
let experimentView = null;

function experimentQuery() {
  const {mode, sid} = experimentView;
  const scope = $("experiment-scope").value;
  return {mode, sid: scope === "unlinked" ? "" : sid, scope: scope === "project" ? scope : "session"};
}

function experimentOwned(record) {
  const {mode, sid} = experimentQuery();
  return record && record.mode === mode && record.sid === sid;
}

function experimentFeedback(message, failed = false) {
  const feedback = $("experiment-feedback");
  feedback.textContent = message;
  feedback.classList.toggle("error", failed);
}

function experimentDate(value) {
  return new Date(value).toLocaleString();
}

function experimentControls() {
  if (!experimentView) return;
  const {busy, record} = experimentView;
  const readOnly = $("experiment-scope").value === "project";
  for (const element of $("experiment-dialog").querySelectorAll("button, select, input, textarea")) {
    element.disabled = busy;
  }
  $("experiment-new").disabled ||= readOnly || state.busy;
  $("experiment-fork").hidden = readOnly;
  $("experiment-fork").disabled ||= state.busy;
  $("experiment-plan").querySelector('[type="submit"]').disabled ||= state.busy;
  const canRegister = !readOnly && experimentOwned(record);
  $("experiment-start").hidden = !canRegister || record?.status !== "planned";
  $("experiment-finish").hidden = !canRegister || record?.status !== "running";
  for (const id of ["experiment-start", "experiment-finish"]) {
    $(id).querySelector('[type="submit"]').disabled ||= state.busy;
  }
}

async function experimentAction(action) {
  if (!experimentView || experimentView.busy) return;
  experimentView.busy = true;
  experimentFeedback("");
  experimentControls();
  try {
    await action();
  } catch (error) {
    experimentFeedback(error.message, true);
  } finally {
    experimentView.busy = false;
    experimentControls();
  }
}

function drawExperimentList() {
  const list = $("experiment-list");
  list.replaceChildren();
  for (const record of experimentView.records) {
    const button = text("button", "");
    button.type = "button";
    if (record.id === experimentView.record?.id && $("experiment-plan").hidden) {
      button.setAttribute("aria-current", "true");
    }
    const access = experimentOwned(record) ? "" : record.shared ? " · 共享" : " · 其他会话";
    button.append(text("span", record.title), text("span",
      `${experimentStatuses[record.status]}${access} · ${experimentDate(record.created)}`, "model-caption"));
    button.onclick = () => experimentAction(async () => {
      await selectExperiment(record.id);
      $("experiment-record-title").focus();
    });
    list.append(button);
  }
  if (!experimentView.records.length) list.append(text("p", "此范围暂无实验记录。", "muted"));
}

function experimentField(list, label, value) {
  list.append(text("dt", label), text("dd", value));
}

function experimentFileList(files) {
  if (!files.length) return text("p", "无", "muted");
  const list = text("ul", "", "experiment-files");
  for (const file of files) {
    const item = text("li", file.path);
    item.append(text("code", file.missing ? "文件缺失" : file.sha256));
    list.append(item);
  }
  return list;
}

function experimentConditionList(report) {
  const list = text("ul", "", "experiment-files");
  for (const change of report.differences) {
    const item = text("li", `${conditionKinds[change.kind]} · ${change.path ?? change.field}`);
    item.append(text("code", `基线 ${change.expected ?? "无"}\n当前 ${change.missing ? "文件缺失" : change.actual ?? "无"}`));
    list.append(item);
  }
  for (const error of report.errors) {
    list.append(text("li", `${conditionKinds[error.kind]} ${error.path ?? ""} · 检查失败（${error.error}）`));
  }
  return list;
}

function drawExperiment(record) {
  const previous = experimentView.record;
  experimentView.record = record;
  $("experiment-plan").hidden = true;
  $("experiment-empty").hidden = Boolean(record);
  $("experiment-record").hidden = !record;
  $("experiment-differences").hidden = true;
  drawExperimentList();
  if (!record) return;

  if (record.id !== previous?.id || record.status !== previous?.status) {
    $("experiment-start").reset();
    $("experiment-finish").reset();
    for (const details of $("experiment-record").querySelectorAll("details")) details.open = false;
  }
  const {spec, baseline, started, outcome, display} = record;
  $("experiment-record-title").textContent = spec.title;
  const access = !record.sid ? "未关联会话" : experimentOwned(record) ? "当前会话" : "其他会话";
  $("experiment-record-meta").textContent = `${experimentStatuses[record.status]} · ${access}${record.shared ? " · 已共享" : ""} · 修订 ${record.revision} · ${experimentDate(baseline.captured_at)}`;
  const summary = $("experiment-summary");
  summary.replaceChildren();
  experimentField(summary, "假设或目标", spec.hypothesis);
  experimentField(summary, "运行命令", spec.command);
  experimentField(summary, "随机种子", display.seed === "null" ? "未填写" : display.seed);
  experimentField(summary, "参数", display.parameters);
  experimentField(summary, "单位", display.units);
  if (spec.hardware_notes) experimentField(summary, "硬件、来源与资源", spec.hardware_notes);
  if (record.parent_id) experimentField(summary, "源计划 ID", record.parent_id);
  experimentField(summary, "实验 ID", record.id);

  const baselineDetails = $("experiment-baseline");
  const warnings = text("ul", "", "muted experiment-files");
  for (const warning of baseline.warnings) warnings.append(text("li", warning));
  const conditions = text("dl", "", "experiment-summary");
  experimentField(conditions, "Git 提交", baseline.git.commit || "未记录");
  experimentField(conditions, "源码差异校验值", baseline.git.selected_diff_sha256 || "未记录");
  experimentField(conditions, "运行环境", `${baseline.environment.implementation} ${baseline.environment.python} · ${baseline.environment.platform}`);
  experimentField(conditions, "Python 包校验值", baseline.environment.package_sha256);
  baselineDetails.replaceChildren(text("h4", "冻结源码"), experimentFileList(baseline.code),
    text("h4", "输入文件"), experimentFileList(baseline.inputs), conditions, warnings);

  $("experiment-outcome-panel").hidden = !started && !outcome;
  const result = $("experiment-outcome");
  result.replaceChildren();
  if (started || outcome) {
    const fields = text("dl", "", "experiment-summary");
    if (started) experimentField(fields, "人工登记开始", experimentDate(started.recorded_at));
    if (outcome) {
      experimentField(fields, "人工登记结果", `${experimentStatuses[outcome.reported_status]} · ${experimentDate(outcome.recorded_at)}`);
      experimentField(fields, "退出码", outcome.reported_exit_code ?? "未填写");
      experimentField(fields, "指标", display.metrics);
      if (outcome.notes) experimentField(fields, "说明", outcome.notes);
      experimentField(fields, "登记结果时的条件", conditionStatuses[outcome.postflight.status]);
    }
    result.append(fields);
    if (outcome) {
      result.append(experimentConditionList(outcome.postflight), text("h4", "结果文件"), experimentFileList(outcome.artifacts),
        text("p", "该结果由用户登记，科学结论仍需独立验证。", "field-hint"));
    }
  }
}

async function selectExperiment(id) {
  const record = await get(`${experimentApi}/${encodeURIComponent(id)}`, {...experimentQuery(), display: "1"});
  drawExperiment(record);
}

async function refreshExperiments(preferred = null) {
  const records = await get(experimentApi, experimentQuery());
  experimentView.records = records;
  drawExperimentList();
  const id = preferred && records.some(record => record.id === preferred) ? preferred : records[0]?.id;
  if (id) await selectExperiment(id);
  else drawExperiment(null);
}

async function openExperiments(preferred = null) {
  if (state.loading || $("experiment-dialog").open) return;
  const sid = state.session || "";
  const title = state.sessions.find(item => item.id === sid)?.title;
  experimentView = {mode: state.mode, sid, records: [], record: null, parent: null, create: null, busy: false};
  closeRail();
  $("experiment-scope-note").textContent = `${experimentModes[state.mode]} · ${title || (sid ? "当前会话" : "未关联会话")}`;
  $("experiment-scope").querySelector('[value="session"]').disabled = !sid;
  $("experiment-scope").value = sid ? "session" : "unlinked";
  $("experiment-list").replaceChildren();
  drawExperiment(null);
  $("experiment-dialog").showModal();
  await experimentAction(() => refreshExperiments(preferred));
}

function editExperimentPlan(source = null) {
  if (state.busy || $("experiment-scope").value === "project") return;
  experimentFeedback("");
  experimentView.parent = source?.id ?? null;
  experimentView.create = null;
  $("experiment-plan").reset();
  $("experiment-plan-heading").textContent = source ? "从现有记录新建计划" : "新建实验计划";
  if (source) {
    const spec = source.spec;
    $("experiment-name").value = spec.title;
    $("experiment-hypothesis").value = spec.hypothesis;
    $("experiment-command").value = spec.command;
    $("experiment-code").value = spec.code_files.join("\n");
    $("experiment-inputs").value = spec.input_files.join("\n");
    $("experiment-seed").value = source.display.seed === "null" ? "" : source.display.seed;
    $("experiment-parameters").value = source.display.parameters;
    $("experiment-units").value = source.display.units;
    $("experiment-hardware").value = spec.hardware_notes;
  }
  $("experiment-empty").hidden = true;
  $("experiment-record").hidden = true;
  $("experiment-plan").hidden = false;
  drawExperimentList();
  experimentControls();
  $("experiment-name").focus();
}

function experimentPaths(id) {
  return $(id).value.split(/\r?\n/).map(path => path.trim()).filter(Boolean);
}

function experimentObject(id, label) {
  const raw = $(id).value.trim() || "{}";
  let parsed;
  try { parsed = JSON.parse(raw); } catch { throw new Error(`${label}须为有效的 JSON 对象。`); }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label}须为 JSON 对象。`);
  }
  return raw;
}

function experimentSeed() {
  const seed = $("experiment-seed").value.trim();
  if (!seed) return "null";
  if (!/^\d+$/.test(seed) || BigInt(seed) > 18446744073709551615n) {
    throw new Error("随机种子须为 0 至 18446744073709551615 的整数。");
  }
  return BigInt(seed).toString();
}

async function postExperiment(suffix, body) {
  const query = new URLSearchParams({...experimentQuery(), display: "1"});
  const response = await fetch(`${experimentApi}${suffix}?${query}`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body,
  });
  if (!response.ok) {
    const error = new Error(await response.text());
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function saveExperiment() {
  const fields = {
    title: $("experiment-name").value.trim(), hypothesis: $("experiment-hypothesis").value.trim(),
    command: $("experiment-command").value.trim(), code_files: experimentPaths("experiment-code"),
    input_files: experimentPaths("experiment-inputs"), hardware_notes: $("experiment-hardware").value.trim(),
    shared: $("experiment-shared").checked,
  };
  const spec = JSON.stringify(fields).slice(0, -1) +
    `,"seed":${experimentSeed()},"parameters":${experimentObject("experiment-parameters", "参数")},"units":${experimentObject("experiment-units", "单位")}}`;
  const parent = JSON.stringify(experimentView.parent);
  const payload = `"spec":${spec},"parent_id":${parent}`;
  if (experimentView.create?.payload !== payload) experimentView.create = {payload, id: crypto.randomUUID()};
  const record = await postExperiment("", `{${payload},"request_id":${JSON.stringify(experimentView.create.id)}}`);
  experimentView.create = null;
  showSavedExperiment(record, "实验基线已冻结。");
}

function showSavedExperiment(record, message) {
  const row = {id: record.id, title: record.spec.title, status: record.status, mode: record.mode, sid: record.sid,
    shared: record.shared, created: record.created};
  const index = experimentView.records.findIndex(item => item.id === record.id);
  if (index === -1) experimentView.records.unshift(row);
  else experimentView.records[index] = row;
  drawExperiment(record);
  experimentFeedback(message);
  $("experiment-record-title").focus();
}

async function registerExperiment(action, acknowledged, result = "") {
  const record = experimentView.record;
  if (!acknowledged || !experimentOwned(record) || $("experiment-scope").value === "project") return;
  const suffix = `/${encodeURIComponent(record.id)}/${action}`;
  try {
    const saved = await postExperiment(suffix, `{"revision":${record.revision},"acknowledged":true${result}}`);
    showSavedExperiment(saved, action === "start" ? "已登记实验开始。" : "已登记实验结果。");
    if (action === "finish") $("experiment-outcome-panel").open = true;
  } catch (error) {
    if (error.status === 409) await refreshExperiments(record.id);
    throw error;
  }
}

async function checkExperiment() {
  const record = experimentView.record;
  const report = await get(`${experimentApi}/${encodeURIComponent(record.id)}/check`, experimentQuery());
  const section = $("experiment-differences");
  const heading = text("h3", conditionStatuses[report.status]);
  heading.id = "experiment-check-title";
  heading.tabIndex = -1;
  section.replaceChildren(heading, text("p", `${experimentDate(report.checked_at)} · 检查范围为冻结时选定的文件、Git 与环境。`, "field-hint"),
    experimentConditionList(report));
  section.hidden = false;
  heading.focus();
}

async function exportExperiment() {
  const {id} = experimentView.record;
  const query = new URLSearchParams(experimentQuery());
  const response = await fetch(`${experimentApi}/${encodeURIComponent(id)}/export?${query}`);
  if (!response.ok) throw new Error(await response.text());
  const url = URL.createObjectURL(await response.blob());
  const download = document.createElement("a");
  download.href = url;
  download.download = `experiment-${id}.zip`;
  document.body.append(download);
  download.click();
  download.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("experiment-nav").onclick = () => openExperiments();
$("experiment-close").onclick = () => $("experiment-dialog").close();
$("experiment-dialog").oncancel = event => { if (experimentView?.busy) event.preventDefault(); };
$("experiment-dialog").onclose = () => { if (!$("experiment-dialog").open) experimentView = null; };
$("experiment-refresh").onclick = () => experimentAction(() => refreshExperiments(experimentView.record?.id));
$("experiment-scope").onchange = () => experimentAction(async () => {
  experimentView.records = [];
  drawExperiment(null);
  await refreshExperiments();
});
$("experiment-new").onclick = () => editExperimentPlan();
$("experiment-fork").onclick = () => editExperimentPlan(experimentView.record);
$("experiment-plan-cancel").onclick = () => {
  drawExperiment(experimentView.record);
  experimentFeedback("");
  experimentControls();
  $("experiment-new").focus();
};
$("experiment-plan").onsubmit = event => {
  event.preventDefault();
  experimentAction(saveExperiment);
};
$("experiment-check").onclick = () => experimentAction(checkExperiment);
$("experiment-export").onclick = () => experimentAction(exportExperiment);
$("experiment-start").onsubmit = event => {
  event.preventDefault();
  experimentAction(() => registerExperiment("start", $("experiment-start-confirm").checked));
};
$("experiment-finish").onsubmit = event => {
  event.preventDefault();
  experimentAction(() => {
    const fields = JSON.stringify({status: $("experiment-result-status").value, exit_code: $("experiment-exit").valueAsNumber,
      artifacts: experimentPaths("experiment-artifacts"), notes: $("experiment-notes").value.trim()});
    const result = `,"result":${fields.slice(0, -1)},"metrics":${experimentObject("experiment-metrics", "结果指标")}}`;
    return registerExperiment("finish", $("experiment-finish-confirm").checked, result);
  });
};
