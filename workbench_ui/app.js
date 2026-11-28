"use strict";

const $ = (id) => document.getElementById(id);
const app = document.querySelector(".app");
const desktopViewer = matchMedia("(min-width: 1100px)");
const tokenFormat = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1});
const state = {
  mode: "agent", taskMode: "agent", session: null, sessions: [], busy: false, loading: false,
  folder: "", file: null, attachments: [], view: 0, fileView: 0, pending: null, stopping: false,
};
let liveSocket = null;
let stopAndContinue = null;
let supplementCards = new Map();
let settingsDraft = null;
let settingsSaving = false;
let memoryDraft = null;
let memorySaving = false;
let knowledgeSaving = false;
let skillsDraft = null;
let skillsBusy = false;
let selectedSkills = [];
const webLive = {job: null, owner: null, epoch: 0, timer: null, polling: null, preview: false, lastImage: 0};
const presets = {
  agent: ["从工作区的 CSV 提取观测摘要", "查询 M31 的基础天体数据", "找几篇关于快速射电暴的近期论文"],
  research: ["为 GRB 余辉观测设计一套研究方案", "梳理快速射电暴宿主星系的研究现状"],
  code: ["检查工作区的 Python 脚本并给出改进建议", "编写读取 FITS 表格的 Python 示例"],
  chat: ["解释红移和宇宙学距离的关系", "怎样判断一条光变曲线是否有周期？"],
};

function text(tag, content, className = "") {
  const element = document.createElement(tag);
  element.textContent = content;
  if (className) element.className = className;
  return element;
}

function icon(name, className = "") {
  const namespace = "http://www.w3.org/2000/svg";
  const element = document.createElementNS(namespace, "svg");
  element.setAttribute("class", `ui-icon ${className}`.trim());
  element.setAttribute("viewBox", "0 0 24 24");
  element.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(namespace, "use");
  use.setAttribute("href", `/ui/assets/icons.svg#${name}`);
  element.append(use);
  return element;
}

function fileIcon(entry) {
  if (entry.folder) return "folder";
  const extension = entry.name.split(".").pop().toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(extension)) return "image";
  if (["csv", "tsv", "ecsv", "xls", "xlsx"].includes(extension)) return "table";
  if (["py", "js", "ts", "css", "html", "json", "yaml", "yml", "sh", "r", "ipynb", "toml"].includes(extension)) return "file-code";
  return "file";
}

async function get(path, params = {}) {
  const query = new URLSearchParams(params);
  const response = await fetch(query.size ? `${path}?${query}` : path);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function atBottom() {
  const box = $("timeline");
  return box.scrollHeight - box.scrollTop - box.clientHeight < 30;
}

function scrollBottom() {
  $("timeline").scrollTop = $("timeline").scrollHeight;
}

function appendMessage(role, value = "", html = false) {
  const message = text("article", "", `message ${role}`);
  if (html) message.innerHTML = value;
  else message.textContent = value;
  $("welcome").hidden = true;
  $("messages").append(message);
  return message;
}

function status(message = "") {
  $("status").textContent = message;
  $("status").title = message;
}

async function checkEngine() {
  try {
    const data = await get("/api/health");
    const engine = data.engine;
    const label = $("engine-status");
    label.hidden = engine.phase === "ready";
    if (engine.phase === "loading") {
      label.textContent = "任务引擎正在后台加载，可以先查看历史或输入消息。";
      setTimeout(checkEngine, 750);
    } else if (engine.phase === "idle") {
      label.textContent = "任务引擎将在首次发送时加载。";
    } else if (engine.phase === "error") {
      label.textContent = `任务引擎加载失败（${engine.error}），请查看启动窗口或发送后查看错误。`;
    }
  } catch {
    // Startup diagnostics must never prevent history or the composer from loading.
    $("engine-status").hidden = true;
  }
}

function showUsage(usage = {}) {
  const {rate, input, limit, cached} = usage;
  $("usage-rate").value = rate == null ? "?" : `${tokenFormat.format(rate)} tok/s`;
  $("usage-context").value = input == null ? "?" : limit
    ? `${tokenFormat.format(input)} / ${tokenFormat.format(limit)} (${tokenFormat.format(100 * input / limit)}%)`
    : `${tokenFormat.format(input)} tok`;
  $("usage-cache").value = cached == null ? "?" : input > 0
    ? `${tokenFormat.format(cached)} tok (${tokenFormat.format(100 * cached / input)}%)`
    : `${tokenFormat.format(cached)} tok`;
}

function controls() {
  const locked = state.busy || state.loading;
  const canSteer = state.busy && liveSocket?.readyState === WebSocket.OPEN && !state.stopping;
  app.classList.toggle("turn-active", state.busy);
  $("top-stop").hidden = !state.busy;
  $("top-stop").disabled = !canSteer;
  $("top-stop").textContent = state.stopping ? "停止中…" : "停止";
  $("send").disabled = (state.busy ? !canSteer : state.loading || Boolean(state.pending)) || !$("model").value || !$("prompt").value.trim();
  $("stop").hidden = !state.busy;
  $("stop").disabled = !canSteer;
  $("interrupt-send").hidden = !state.busy;
  $("interrupt-send").disabled = !canSteer || !$("prompt").value.trim();
  $("stop").textContent = state.stopping ? "停止中…" : "停止";
  $("send").classList.toggle("steering", state.busy);
  $("send").setAttribute("aria-label", state.busy ? "补充指令" : "发送");
  $("send").title = state.busy ? "将补充指令加入当前任务，在下个安全模型步骤生效" : "发送（Enter）";
  $("send").replaceChildren(state.busy ? document.createTextNode("补充指令") : icon("send"));
  $("prompt").placeholder = state.busy ? "输入补充指令，下个安全步骤生效；需要立即调整可选停止并补充…" : "描述你要完成的任务…";
  for (const id of ["mode", "model", "new", "nav-task", "nav-chat"]) $(id).disabled = locked;
  $("experiment-nav").disabled = state.loading;
  $("qwen-reasoning").hidden = $("model").selectedOptions[0]?.dataset.qwen !== "true" || Boolean(state.pending);
  $("qwen-reasoning").disabled = locked || Boolean(state.pending);
  for (const button of $("sessions").querySelectorAll("button")) button.disabled = locked;
  $("attach").hidden = state.mode !== "chat";
  $("attach").disabled = locked || state.mode !== "chat" || !state.file?.attach || state.attachments.length >= 8;
  $("settings-save").disabled = locked || settingsSaving;
  $("memory-open").disabled = locked || !state.session;
  $("skills-open").disabled = locked || Boolean(state.pending);
  memoryControls();
  if (typeof experimentControls === "function") experimentControls();
}

function drawSkillSelection() {
  $("skills-selected").hidden = !selectedSkills.length;
  $("skills-selected").textContent = selectedSkills.length ? `本轮技能：${selectedSkills.map(skill => skill.name).join("、")}` : "";
}

function skillsFeedback(message, failed=false) {
  $("skills-feedback").textContent = message;
  $("skills-feedback").classList.toggle("error", failed);
}

function skillControls() {
  for (const id of ["skills-refresh", "skills-official", "skills-upload", "skills-zip-name"]) $(id).disabled = skillsBusy;
  $("skills-import-official").disabled = skillsBusy || !$("skills-official-name").value;
  $("skills-official-name").disabled = skillsBusy || !$("skills-official-name").value;
  $("skill-enabled").disabled = skillsBusy || !skillsDraft?.detail;
  $("skill-selected").disabled = skillsBusy || !skillsDraft?.detail?.enabled;
}

function drawSkills() {
  $("skills-list").replaceChildren();
  for (const skill of skillsDraft?.skills || []) {
    const button = text("button", "");
    button.type = "button";
    button.append(text("span", skill.name), text("span", skill.changed ? "内容变更，需重新审阅" : skill.enabled ? "已启用" : "未启用", "model-caption"));
    button.setAttribute("aria-current", String(skill.name === skillsDraft?.detail?.name));
    button.onclick = () => showSkill(skill.name);
    button.disabled = skillsBusy;
    $("skills-list").append(button);
  }
  $("skills-empty").hidden = Boolean(skillsDraft?.detail);
}

async function refreshSkills(preferred) {
  const data = await get("/api/skills");
  const previous = preferred || skillsDraft?.detail?.name;
  skillsDraft = {...data, detail: null};
  $("skills-directory").textContent = data.directory;
  selectedSkills = selectedSkills.filter(entry => data.skills.some(skill => skill.enabled && skill.name === entry.name && skill.checksum === entry.checksum));
  drawSkillSelection();
  drawSkills();
  $("skills-detail").hidden = true;
  if (previous && data.skills.some(skill => skill.name === previous)) await showSkill(previous);
  skillControls();
  if (data.warnings.length) skillsFeedback(data.warnings.join("；"), true);
}

async function showSkill(name) {
  const draft = skillsDraft;
  const request = draft.request = (draft.request || 0) + 1;
  try {
    const detail = await get(`/api/skills/${encodeURIComponent(name)}`);
    if (skillsDraft !== draft || draft.request !== request || !$("skills-dialog").open) return;
    draft.detail = detail;
    $("skills-detail").hidden = false;
    $("skills-empty").hidden = true;
    $("skill-name").textContent = detail.name;
    $("skill-description").textContent = detail.description;
    $("skill-origin").textContent = `许可证：${detail.license} · 来源：${detail.source?.repository || "项目本地 / ZIP"}${detail.source?.archive_sha256 ? " · 下载包 SHA256：" + detail.source.archive_sha256 : ""}`;
    $("skill-compatibility").textContent = detail.compatibility ? `环境要求：${detail.compatibility}` : "依赖请按技能说明核对；导入不保证外部命令或 Python 包已安装。";
    $("skill-checksum").textContent = `包校验值：${detail.checksum}`;
    $("skill-enabled").textContent = detail.enabled ? "禁用后续加载" : "已审阅，启用此版本";
    $("skill-selected").checked = selectedSkills.some(entry => entry.name === name && entry.checksum === detail.checksum);
    $("skill-content").textContent = detail.text;
    $("skill-resources").replaceChildren(...Object.entries(detail.files).map(([path, meta]) => text("li", `${path} · ${meta.bytes} bytes`)));
    drawSkills();
    skillControls();
    if (matchMedia("(max-width: 680px)").matches) $("skills-detail").scrollIntoView({block:"start"});
  } catch (error) {skillsFeedback(error.message, true);}
}

async function openSkills() {
  if ($("skills-dialog").open) return;
  $("skills-dialog").showModal();
  skillsFeedback("正在读取项目技能…");
  try {await refreshSkills(); skillsFeedback("查看完整 SKILL.md、来源和许可证后再启用；不会自动执行包内脚本。");}
  catch (error) {skillsFeedback(error.message, true);}
}

async function importSkill(payload, name) {
  if (skillsBusy) return;
  skillsBusy = true; skillControls(); drawSkills();
  skillsFeedback("正在导入技能，只复制说明和资源…");
  try {
    const response = await fetch(`/api/skills/import${name ? "?name=" + encodeURIComponent(name) : ""}`, payload);
    if (!response.ok) throw new Error(await response.text());
    const imported = await response.json();
    await refreshSkills(imported.name);
    skillsFeedback(`已导入 ${imported.name}，默认未启用；请审阅说明、资源和许可证。`);
  } catch (error) {skillsFeedback(error.message, true);}
  finally {skillsBusy = false; skillControls(); drawSkills();}
}

function memoryFeedback(message, failed = false) {
  $("memory-feedback").textContent = message;
  $("memory-feedback").classList.toggle("error", failed);
}

function memoryControls() {
  const locked = state.busy || state.loading || Boolean(state.pending) || memorySaving || knowledgeSaving || !memoryDraft;
  $("memory-save").disabled = locked || !memoryDraft?.detail?.compatible;
  $("memory-restore").disabled = locked || !memoryDraft?.detail?.compatible;
  $("memory-reset").disabled = locked || !memoryDraft?.data?.active_version;
  $("memory-summary").disabled = memorySaving || !memoryDraft?.detail?.compatible;
  $("memory-close").disabled = memorySaving || knowledgeSaving;
  for (const id of ["knowledge-save", "knowledge-new", "knowledge-reindex"]) $(id).disabled = locked;
  for (const button of $("knowledge-list").querySelectorAll("button")) button.disabled = locked;
}

function knowledgeFeedback(message, failed = false) {
  $("knowledge-feedback").textContent = message;
  $("knowledge-feedback").classList.toggle("error", failed);
}

function knowledgePath() {
  return `/api/sessions/${encodeURIComponent(memoryDraft.sid)}/knowledge?${new URLSearchParams({mode: memoryDraft.mode})}`;
}

async function loadSubagents() {
  if (!memoryDraft) return;
  const draft = memoryDraft;
  $("subagent-feedback").textContent = "正在读取子任务…";
  try {
    const path = `/api/sessions/${encodeURIComponent(draft.sid)}/subagents`;
    const rows = await get(path, {mode: draft.mode});
    if (memoryDraft !== draft) return;
    $("subagent-list").replaceChildren();
    for (const job of rows) {
      const button = text("button", "", "knowledge-card");
      button.type = "button";
      const name = {literature:"文献检索", verification:"证据核验", testing:"测试编写"}[job.kind] || job.kind;
      button.append(text("strong", name), text("p", `${job.status} · ${new Date(job.created).toLocaleString()}`, "muted"));
      button.onclick = async () => {
        try {
          const detail = await get(`${path}/${encodeURIComponent(job.job_id)}`, {mode: draft.mode});
          if (memoryDraft !== draft) return;
          $("subagent-report").hidden = false;
          $("subagent-report").textContent = detail.report ? formatSubagentReport(detail.report) : "尚未保存完整报告，不能作为已完成结果。";
        } catch (error) { if (memoryDraft === draft) $("subagent-feedback").textContent = error.message; }
      };
      $("subagent-list").append(button);
    }
    $("subagent-feedback").textContent = rows.length ? `${rows.length} 个子任务；点击查看来源、提案、审查结论和预算。` : "暂无子任务。可请求文献检索、证据核验，或在 Code/Agent 中让测试子 Agent 编写测试提案。";
  } catch (error) { if (memoryDraft === draft) $("subagent-feedback").textContent = error.message; }
}

function formatSubagentReport(report) {
  const statuses = {complete: "已完成", partial: "部分完成 / 证据不足", failed: "失败", budget_exhausted: "达到预算上限", interrupted: "中断", proposal_ready:"测试提案已生成 / 未执行", stale_proposal:"源码已变更 / 提案需重新核对"};
  const verdicts = {supported: "来源支持", contradicted: "来源矛盾", insufficient: "证据不足"};
  const lines = [`任务 ${report.job_id}`, `状态：${statuses[report.status] || report.status}`, "", "辅助综述（未独立核实）", report.summary_unverified || "暂无", "", report.verification_scope || ""];
  if (report.kind === "testing") {
    if (report.proposal) lines.push("", `拟新增测试：${report.proposal.path}`, `SHA-256：${report.proposal.sha256}`, "", report.proposal.content);
    lines.push("", `语法检查：${report.verification?.syntax || "未检查"}`, "实际测试：未执行；RED/GREEN 未验证。", "应用测试和运行命令仍需主 Agent 的原有审批。");
    if (report.model) lines.push(`模型渠道：${report.model.name} · ${report.model.single_concurrency ? "此 Qwen 服务请求单并发" : "不额外限制为单并发"}`);
    for (const [path, checksum] of Object.entries(report.source_revisions || {})) lines.push(`源码快照：${path} · ${checksum}`);
  }
  for (const finding of report.findings || []) {
    lines.push("", `说法：${finding.claim}`, `审查：${verdicts[finding.verdict] || finding.verdict}`);
    for (const evidence of finding.evidence || []) lines.push(`[${evidence.source_id}] ${evidence.quote}`);
  }
  lines.push("", "实际取得的来源");
  for (const source of report.sources || []) {
    lines.push(`[${source.source_id}] ${source.title || source.identifier || source.original_reference || source.source_role}`);
    if (source.identifier) lines.push(`标识：${source.identifier}`);
    if (source.url) lines.push(source.url);
    if (source.archive_reference) lines.push(`原文档案：${source.archive_reference}`);
    lines.push(`SHA-256：${source.checksum}`, "");
  }
  if (report.budget) lines.push(`模型调用：${report.budget.model_calls}/${report.budget.max_model_calls} · 工具尝试：${report.budget.tool_calls}/${report.budget.max_tool_calls}`);
  lines.push(`允许工具：${(report.allowed_tools || []).join(", ")}`);
  if (report.warnings?.length) lines.push("", "提示", ...report.warnings);
  if (report.open_questions?.length) lines.push("", "待核验问题", ...report.open_questions);
  return lines.join("\n");
}

function editKnowledge(entry = null) {
  if (!memoryDraft || knowledgeSaving) return;
  memoryDraft.knowledgeEdit = entry;
  $("knowledge-entry-id").value = entry?.entry_id || "";
  $("knowledge-revision").value = entry?.revision || "";
  $("knowledge-title").value = entry?.title || "";
  $("knowledge-keywords").value = entry?.keywords.join(", ") || "";
  $("knowledge-reference").value = entry?.source_reference || "";
  $("knowledge-quote").value = entry?.quote || "";
  $("knowledge-note").value = entry?.note || "";
  $("knowledge-priority").value = entry?.priority ?? 50;
  $("knowledge-shared").checked = entry?.scope === "project";
  $("knowledge-origin").textContent = `来源会话：${entry?.source_mode || memoryDraft.mode} · ${entry?.source_session || memoryDraft.sid}`;
  $("knowledge-editor").open = true;
}

function drawKnowledge() {
  $("knowledge-list").replaceChildren();
  for (const entry of memoryDraft?.knowledgeRows || []) {
    const card = text("article", "", "knowledge-card");
    card.append(text("strong", entry.title), text("p", `${entry.enabled ? "已启用" : "已停用"} · ${entry.scope === "project" ? "项目共享" : "当前会话"} · 修订 ${entry.revision} · 优先级 ${entry.priority}`, "muted"),
                text("p", `触发词：${entry.keywords.join(" / ")}`), text("p", entry.source_status, "muted"),
                text("p", `${entry.source_mode}:${entry.source_session}:${entry.source_reference}`, "muted"));
    const actions = text("div", "", "approval-actions");
    const edit = text("button", "编辑", "small-button");
    edit.type = "button";
    edit.onclick = () => editKnowledge(entry);
    const toggle = text("button", entry.enabled ? "停用" : "启用", "small-button");
    toggle.type = "button";
    toggle.onclick = () => updateKnowledge({action: "toggle", entry_id: entry.entry_id, revision: entry.revision, enabled: !entry.enabled});
    actions.append(edit, toggle);
    card.append(actions);
    $("knowledge-list").append(card);
  }
  if (!memoryDraft?.knowledgeRows?.length) $("knowledge-list").append(text("p", "尚无知识条目。先检索原文，再选择来源保存。", "muted"));
  memoryControls();
}

async function searchKnowledge(event) {
  event?.preventDefault();
  if (!memoryDraft) return;
  const draft = memoryDraft, request = ++draft.searchRequest;
  knowledgeFeedback("正在检索原文…");
  try {
    const data = await get(memoryPath("/search"), {mode: draft.mode, q: $("knowledge-query").value, history: $("knowledge-history").checked ? "1" : "0"});
    if (memoryDraft !== draft || draft.searchRequest !== request) return;
    $("knowledge-hits").replaceChildren();
    for (const hit of data.hits) {
      const card = text("article", "", "knowledge-card");
      card.append(text("p", `${hit.reference} · ${hit.role}${hit.historical ? " · 历史资料" : ""}`, "muted"), text("p", hit.snippet, "knowledge-excerpt"));
      const actions = text("div", "", "approval-actions");
      for (const [label, use] of [["查看原文", false], ["用作知识来源", true]]) {
        const button = text("button", label, "small-button");
        button.type = "button";
        button.disabled = use && (state.busy || Boolean(state.pending));
        button.onclick = async () => {
          try {
            const source = await get(memoryPath("/source"), {mode: draft.mode, ref: hit.reference});
            if (memoryDraft !== draft) return;
            $("knowledge-source-panel").hidden = false;
            $("knowledge-source-panel").open = true;
            $("knowledge-source").textContent = `${source.reference}\nSHA-256 ${source.checksum}\n\n${source.content}`;
            if (use) {
              editKnowledge();
              $("knowledge-reference").value = hit.reference;
              $("knowledge-quote").value = hit.snippet;
              $("knowledge-title").value = $("knowledge-query").value.slice(0, 120);
              $("knowledge-keywords").value = $("knowledge-query").value;
              knowledgeFeedback("已选择原文来源；可缩短摘录，但不要改写。解释与推断写入备注。" );
            }
          } catch (error) { if (memoryDraft === draft) knowledgeFeedback(error.message, true); }
        };
        actions.append(button);
      }
      card.append(actions);
      $("knowledge-hits").append(card);
    }
    knowledgeFeedback(`${data.engine === "fts5" ? "FTS5" : "字面检索"} · ${data.hits.length} 条来源${data.warnings.length ? " · " + data.warnings.join("；") : ""}`, Boolean(data.warnings.length));
  } catch (error) {
    if (memoryDraft === draft) knowledgeFeedback(error.message, true);
  }
}

async function updateKnowledge(payload) {
  if (!memoryDraft || knowledgeSaving || state.busy || state.pending) return;
  const draft = memoryDraft;
  knowledgeSaving = true;
  memoryControls();
  knowledgeFeedback("正在保存知识条目…");
  try {
    const response = await fetch(knowledgePath(), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    if (!response.ok) throw new Error(await response.text());
    draft.knowledgeRows = await response.json();
    drawKnowledge();
    knowledgeFeedback(payload.action === "toggle" ? "知识启用状态已保存。" : "知识条目已保存；仅在触发词命中时注入。" );
    if (payload.action === "save") {
      $("knowledge-editor").open = false;
      $("knowledge-source-panel").open = false;
    }
  } catch (error) {
    knowledgeFeedback(`${error.message} 编辑内容仍保留。`, true);
  } finally {
    knowledgeSaving = false;
    memoryControls();
  }
}

function memoryPath(suffix = "") {
  return `/api/sessions/${encodeURIComponent(memoryDraft.sid)}/memory${suffix}`;
}

function drawMemory() {
  $("memory-versions").replaceChildren();
  $("memory-blocks").replaceChildren();
  const {data} = memoryDraft;
  data.versions.forEach((version, index) => {
    const button = text("button", "");
    button.type = "button";
    button.setAttribute("aria-current", String(version.version_id === memoryDraft.selected));
    const active = version.version_id === data.active_version ? " · 使用中" : "";
    const kind = {edit: "手动修订", compact: "压缩", migration: "旧版迁移"}[version.kind] || version.kind;
    button.append(text("span", `v${data.versions.length - index} · ${kind}${active}`),
                  text("span", `${version.prefix_count} 条原文 · ${new Date(version.created).toLocaleString()}`, "model-caption"));
    if (!version.compatible) button.append(text("span", version.integrity ? "原文已修订，仅供查看" : "完整性校验失败", "model-caption"));
    button.onclick = () => selectMemory(version.version_id);
    $("memory-versions").append(button);
  });
  if (!data.versions.length) $("memory-versions").append(text("p", "尚无摘要，可先用 /compact。", "muted"));
  const originals = text("button", "查看当前完整原文");
  originals.type = "button";
  originals.onclick = () => selectMemory(null);
  $("memory-blocks").append(originals);
  for (const block of data.blocks) {
    const button = text("button", `${block.kind} · ${block.block_id.slice(0, 10)}`);
    button.type = "button";
    button.title = `${block.thread_id}\n${block.created}`;
    button.onclick = () => selectMemoryBlock(block);
    $("memory-blocks").append(button);
  }
}

function drawMemorySources(data) {
  $("memory-sources").textContent = data.sources.map(source =>
    `[${source.idx + 1}] ${source.role} · ${source.message_id} · 修订 ${source.revision}\n${source.content}`
  ).join("\n\n") || "没有原文消息。";
  const start = memoryDraft.offset;
  $("memory-page").textContent = `${data.source_count ? start + 1 : 0}–${Math.min(start + data.sources.length, data.source_count)} / ${data.source_count}`;
  $("memory-prev").disabled = start === 0;
  $("memory-next").disabled = start + data.sources.length >= data.source_count;
  $("memory-page").parentElement.hidden = false;
}

async function selectMemory(vid, offset = 0) {
  if (!memoryDraft || memorySaving) return;
  const draft = memoryDraft;
  if (draft.selected && draft.detail) draft.edits[draft.selected] = $("memory-summary").value;
  const request = ++draft.request;
  try {
    const data = await get(memoryPath(vid ? `/versions/${encodeURIComponent(vid)}` : "/originals"), {mode: draft.mode, offset});
    if (memoryDraft !== draft || draft.request !== request) return;
    draft.selected = vid;
    draft.detail = vid ? data : null;
    draft.offset = offset;
    $("memory-summary").value = vid ? draft.edits[vid] ?? data.summary : "";
    $("memory-summary").hidden = !vid;
    $("memory-summary").previousElementSibling.hidden = !vid;
    $("memory-diff-section").hidden = !vid;
    $("memory-diff").textContent = data.diff || "与父版本相同（或首个版本）。";
    $("memory-meta").textContent = vid
      ? `${data.version_id} · 来源 SHA-256 ${data.source_hash} · 模型 ${data.model || "未记录"}`
      : `当前完整原文 · ${data.source_count} 条消息；历史摘要不会替代这些记录。`;
    $("memory-sources-section").open = !vid;
    drawMemorySources(data);
    drawMemory();
    memoryControls();
  } catch (error) {
    if (memoryDraft === draft && draft.request === request) memoryFeedback(error.message, true);
  }
}

async function selectMemoryBlock(block) {
  if (!memoryDraft || memorySaving) return;
  const draft = memoryDraft, request = ++draft.request;
  if (draft.selected && draft.detail) draft.edits[draft.selected] = $("memory-summary").value;
  try {
    const data = await get(memoryPath(`/blocks/${encodeURIComponent(block.block_id)}`), {mode: draft.mode});
    if (memoryDraft !== draft || draft.request !== request) return;
    draft.selected = null;
    draft.detail = null;
    $("memory-summary").hidden = true;
    $("memory-summary").previousElementSibling.hidden = true;
    $("memory-diff-section").hidden = true;
    $("memory-meta").textContent = `${block.block_id} · 任务 ${block.run_id} · ${block.thread_id}`;
    $("memory-sources").textContent = JSON.stringify(data, null, 2);
    $("memory-sources-section").open = true;
    $("memory-page").parentElement.hidden = true;
    drawMemory();
    memoryControls();
  } catch (error) {
    if (memoryDraft === draft && draft.request === request) memoryFeedback(error.message, true);
  }
}

async function openMemory() {
  if (!state.session || state.busy || state.loading) return;
  memoryDraft = {sid: state.session, mode: state.mode, data: null, detail: null, selected: null, offset: 0, request: 0, edits: {}, knowledgeRows: [], knowledgeEdit: null, searchRequest: 0};
  const draft = memoryDraft;
  $("memory-dialog").showModal();
  $("memory-versions").replaceChildren();
  $("memory-blocks").replaceChildren();
  $("memory-summary").value = "";
  $("memory-meta").textContent = "";
  $("memory-sources").textContent = "";
  $("knowledge-hits").replaceChildren();
  $("knowledge-list").replaceChildren();
  $("knowledge-source-panel").hidden = true;
  $("knowledge-query").value = "";
  $("knowledge-history").checked = false;
  $("research-memory").open = false;
  $("subagent-records").open = false;
  $("subagent-list").replaceChildren();
  $("subagent-report").hidden = true;
  editKnowledge();
  $("knowledge-editor").open = false;
  memoryFeedback("正在读取记忆…");
  memoryControls();
  try {
    const data = await get(memoryPath(), {mode: draft.mode});
    if (memoryDraft !== draft) return;
    draft.data = data;
    drawMemory();
    await selectMemory(data.active_version || data.versions[0]?.version_id || null);
    const entries = await get(`/api/sessions/${encodeURIComponent(draft.sid)}/knowledge`, {mode: draft.mode});
    if (memoryDraft !== draft) return;
    draft.knowledgeRows = entries;
    drawKnowledge();
    knowledgeFeedback("关键词触发，默认当前会话；项目共享需要明确勾选。" );
    await loadSubagents();
    memoryFeedback(state.pending ? "有待处理任务：可查看原文，完成或放弃任务后才能更改摘要。" : "保存修订会创建新版本；关闭面板不会影响输入草稿或附件。" );
  } catch (error) {
    if (memoryDraft === draft) memoryFeedback(error.message, true);
  }
}

async function changeMemory(action, reset = false) {
  if (!memoryDraft?.data || memorySaving || state.busy || state.pending) return;
  if (action === "restore" && !confirm(reset ? "切换为完整原文上下文？长对话可能超出模型上限，原文和摘要版本均会保留。" : "恢复此摘要用于下一轮？此版本之后的消息仍会完整保留。")) return;
  const draft = memoryDraft;
  memorySaving = true;
  memoryControls();
  memoryFeedback("正在保存…");
  try {
    const response = await fetch(`${memoryPath()}?${new URLSearchParams({mode: draft.mode})}`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action, version_id: reset ? null : draft.selected,
                            summary: $("memory-summary").value,
                            token: {revision: draft.data.revision, active_version: draft.data.active_version}}),
    });
    if (!response.ok) throw new Error(await response.text());
    draft.data = await response.json();
    draft.edits = {};
    draft.selected = null;
    draft.detail = null;
    memorySaving = false;
    drawMemory();
    await selectMemory(draft.data.active_version || null);
    memoryFeedback(action === "edit" ? "修订已保存为新版本并启用，原文未改动。" : "上下文已切换，原文和后续消息均未删除。" );
  } catch (error) {
    memoryFeedback(`${error.message} 当前编辑内容仍保留。`, true);
  } finally {
    memorySaving = false;
    memoryControls();
  }
}

function drawSessions() {
  $("sessions").replaceChildren();
  for (const session of state.sessions) {
    const button = text("button", "", "session");
    button.title = session.title;
    button.setAttribute("aria-current", String(session.id === state.session));
    button.append(icon(state.mode === "chat" ? "chat" : "task", "session-icon"), text("span", session.title, "label"));
    if (session.pending) {
      const marker = text("span", "•", "pending-dot");
      marker.title = "有待处理任务";
      marker.setAttribute("aria-label", "有待处理任务");
      button.append(marker);
    }
    button.onclick = () => openSession(session.id);
    $("sessions").append(button);
  }
  $("title").textContent = state.sessions.find((item) => item.id === state.session)?.title ||
    (state.mode === "chat" ? "新对话" : "新任务");
  controls();
}

async function loadState(view = state.view) {
  const data = await get("/api/state", {mode: state.mode});
  if (view !== state.view) return;
  state.sessions = data.sessions;
  $("root").textContent = data.workspace;
  $("root").title = data.workspace;
  const previous = $("model").value || localStorage.getItem("astro-profile");
  $("model").replaceChildren();
  for (const profile of data.profiles) {
    const option = new Option(`${profile.label || profile.id} · ${profile.model}`, profile.id);
    option.dataset.qwen = String(profile.model.toLowerCase() === "qwen3.8-27b-coder390");
    $("model").append(option);
  }
  if (data.profiles.some((profile) => profile.id === previous)) $("model").value = previous;
  if (!data.profiles.length) $("model").append(new Option("未配置模型", ""));
  $("hint").textContent = data.profiles.length ? "Enter 发送 · Shift + Enter 换行 · /compact 压缩上下文" : "点击设置，添加模型 URL、Key 和模型 ID";
  drawSessions();
}

function settingsFeedback(message, failed = false) {
  $("settings-feedback").textContent = message;
  $("settings-feedback").classList.toggle("error", failed);
}

const webAdvisorStatuses = {prepared:"等待外发审批", approved:"已批准", connecting:"连接中", sending:"文本进入网页", sent:"等待网页回复", complete:"建议已返回 / 尚未核验", rejected:"已拒绝 / 未发送", not_sent:"未发送", requires_user:"需要手动处理网页", busy:"队列忙 / 未发送", uncertain:"结果未知 / 队列锁定", resolved:"用户已核实 / 队列已解除"};

function webLiveVisible() {
  return !$("panel").hidden && !$("web-live").hidden && !document.hidden;
}

function stopWebLive(clear = false) {
  webLive.epoch++;
  clearTimeout(webLive.timer);
  webLive.preview = false;
  for (const id of ["web-live-image", "web-live-dialog-image"]) $(id).removeAttribute("src");
  $("web-live-image").hidden = true;
  $("web-live-enlarge").hidden = true;
  $("web-live-preview").textContent = "显示官网画面（只读）";
  $("web-live-preview").setAttribute("aria-pressed", "false");
  if ($("web-live-dialog").open) $("web-live-dialog").close();
  if (clear) {
    webLive.job = null;
    webLive.owner = null;
    $("web-live-jobs").replaceChildren();
    $("web-live-detail").hidden = true;
  }
}

async function webLiveAction(path) {
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({confirmed:true})});
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function checkWebLiveConnection() {
  $("web-live-connect").disabled = true;
  $("web-live-connection").textContent = "检查本机 CDP 控制连接…";
  try {
    const info = await get("/api/web-advisors/browser");
    $("web-live-connection").textContent = `${info.cdp_endpoint} · ${info.connected ? "已连接" : "未连接"}\n${info.message}`;
  } catch (error) { $("web-live-connection").textContent = error.message; }
  finally { $("web-live-connect").disabled = false; }
}

async function refreshWebLive(preferred = null) {
  const sid = state.session, mode = state.mode, epoch = webLive.epoch;
  if (!sid) {
    $("web-live-status").textContent = "当前还没有会话。Code/Agent 发起顾问任务后即可查看；可先打开官网登录。";
    return;
  }
  try {
    const jobs = await get(`/api/sessions/${encodeURIComponent(sid)}/web-advisors`, {mode});
    if (epoch !== webLive.epoch || state.session !== sid || state.mode !== mode || !webLiveVisible()) return;
    const provider = $("web-live-provider").value;
    const visible = jobs.filter(job => provider === "all" || job.provider === provider);
    $("web-live-jobs").replaceChildren();
    for (const job of visible) {
      const button = text("button", `${job.provider} · ${webAdvisorStatuses[job.status] || job.status}`, "knowledge-card");
      button.type = "button";
      button.dataset.job = job.job_id;
      button.setAttribute("aria-current", String(job.job_id === webLive.job));
      button.onclick = () => selectWebLiveJob(job.job_id, sid, mode);
      $("web-live-jobs").append(button);
    }
    if (!visible.length) {
      stopWebLive();
      webLive.job = null;
      $("web-live-detail").hidden = true;
      $("web-live-status").textContent = "当前会话暂无该网页顾问任务。请求会先进入外发审批，不会在此自动发送问题。";
    } else {
      const chosen = visible.find(job => job.job_id === preferred) || visible.find(job => job.job_id === webLive.job) || visible[0];
      if (chosen.job_id !== webLive.job || webLive.owner?.sid !== sid || webLive.owner?.mode !== mode) selectWebLiveJob(chosen.job_id, sid, mode);
    }
  } catch (error) { if (epoch === webLive.epoch) $("web-live-status").textContent = error.message; }
}

function selectWebLiveJob(job, sid = state.session, mode = state.mode) {
  stopWebLive();
  webLive.job = job;
  webLive.owner = {sid, mode};
  $("web-live-request").textContent = "";
  $("web-live-answer").textContent = "";
  $("web-live-image-status").textContent = "官网镜像未开启。实际交互请打开官网窗口。";
  $("web-live-detail").hidden = false;
  pollWebLive(webLive.epoch);
}

async function pollWebLive(epoch) {
  const owner = webLive.owner, job = webLive.job;
  if (!owner || !job || epoch !== webLive.epoch || !webLiveVisible() || webLive.polling === epoch) return;
  clearTimeout(webLive.timer);
  webLive.polling = epoch;
  const valid = () => epoch === webLive.epoch && state.session === owner.sid && state.mode === owner.mode && webLiveVisible();
  const path = `/api/sessions/${encodeURIComponent(owner.sid)}/web-advisors/${encodeURIComponent(job)}`;
  try {
    const detail = await get(path, {mode:owner.mode});
    if (!valid()) return;
    const status = webAdvisorStatuses[detail.status] || detail.status;
    $("web-live-status").textContent = `${detail.request.provider} · ${status}\n${job}`;
    $("web-live-request").textContent = detail.payload.body;
    $("web-live-answer").textContent = detail.report?.answer || detail.report?.partial_answer || detail.report?.message || "等待网页回复…";
    for (const button of $("web-live-jobs").querySelectorAll("button")) {
      button.setAttribute("aria-current", String(button.dataset.job === job));
      if (button.dataset.job === job) button.textContent = `${detail.request.provider} · ${status}`;
    }
    $("web-live-focus").disabled = !detail.report?.browser?.target_id;
    if (webLive.preview && Date.now() - webLive.lastImage >= 1800) {
      webLive.lastImage = Date.now();
      try {
        const shot = await webLiveAction(`${path}/preview?${new URLSearchParams({mode:owner.mode})}`);
        if (!valid() || !webLive.preview) return;
        // Only a bounded JPEG data URL is accepted, never page HTML.
        if (!/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(shot.image) || shot.image.length > 1400100) throw new Error("官网画面格式不正确");
        $("web-live-image").src = shot.image;
        $("web-live-dialog-image").src = shot.image;
        $("web-live-image").hidden = false;
        $("web-live-enlarge").hidden = false;
        $("web-live-image-status").textContent = `只读镜像 · ${new Date().toLocaleTimeString()} · 未保存 / 未发送给模型`;
      } catch (error) {
        if (!valid()) return;
        webLive.preview = false;
        $("web-live-preview").textContent = "显示官网画面（只读）";
        $("web-live-preview").setAttribute("aria-pressed", "false");
        for (const id of ["web-live-image", "web-live-dialog-image"]) $(id).removeAttribute("src");
        $("web-live-image").hidden = true;
        $("web-live-enlarge").hidden = true;
        if ($("web-live-dialog").open) $("web-live-dialog").close();
        $("web-live-image-status").textContent = error.message;
      }
    }
  } catch (error) { if (valid()) $("web-live-status").textContent = error.message; }
  finally {
    if (webLive.polling === epoch) webLive.polling = null;
    if (valid()) webLive.timer = setTimeout(() => pollWebLive(epoch), 1000);
  }
}

function openWebLivePanel() {
  closeRail();
  if (!desktopViewer.matches || app.classList.contains("viewer-expanded")) showViewer(false);
  showPanel(true);
  showTab("web");
}

function showWebAdvisorSettings(config = {}) {
  $("settings-web-enabled").checked = Boolean(config.enabled);
  $("settings-web-cdp").value = config.cdp_endpoint || "http://127.0.0.1:9222";
  $("settings-web-timeout").value = config.timeout ?? 180;
  $("settings-web-idle").value = config.auto_close_minutes ?? 0;
  $("settings-web-browser").value = config.browser_type || "dedicated";
  $("settings-web-proxy-mode").value = config.proxy_mode || "system";
  $("settings-web-proxy-url").value = config.proxy_url || "";
  $("settings-web-fingerprint-confirmed").checked = Boolean(config.fingerprint_proxy_confirmed);
  $("settings-web-dedicated").checked = Boolean(config.dedicated_profile);
  $("settings-web-chatgpt").checked = (config.providers || ["chatgpt", "claude"]).includes("chatgpt");
  $("settings-web-claude").checked = (config.providers || ["chatgpt", "claude"]).includes("claude");
  $("settings-web-gemini").checked = (config.providers || ["chatgpt", "claude", "gemini"]).includes("gemini");
  updateWebAdvisorBrowserFields();
}

function updateWebAdvisorBrowserFields(changed = false) {
  const fingerprint = $("settings-web-browser").value === "fingerprint";
  const custom = $("settings-web-proxy-mode").value === "custom";
  $("settings-web-proxy-field").hidden = !custom;
  $("settings-web-proxy-url").disabled = !custom || settingsSaving;
  $("settings-web-proxy-url").required = custom;
  $("settings-web-fingerprint-confirm-field").hidden = !fingerprint;
  $("settings-web-fingerprint-confirmed").disabled = !fingerprint || settingsSaving;
  if (changed) $("settings-web-fingerprint-confirmed").checked = false;
  $("settings-web-browser-hint").textContent = fingerprint
    ? "手动启动独立指纹浏览器配置，将其本机 CDP 地址填到下方。支持提供标准 CDP 的 Chromium 内核；尚未接入具体厂商 API，也不自动改变指纹。"
    : "手动运行项目的启动网页顾问.bat，使用独立 Chrome/Edge 登录窗口。修改代理后需关闭这个专用窗口并重新启动，不会自动停止其他浏览器。";
}

function captureWebAdvisorSettings() {
  return {enabled: $("settings-web-enabled").checked, cdp_endpoint: $("settings-web-cdp").value.trim(),
    timeout: Number($("settings-web-timeout").value), dedicated_profile: $("settings-web-dedicated").checked,
    auto_close_minutes: Number($("settings-web-idle").value),
    browser_type: $("settings-web-browser").value, proxy_mode: $("settings-web-proxy-mode").value,
    proxy_url: $("settings-web-proxy-url").value.trim(), fingerprint_proxy_confirmed: $("settings-web-fingerprint-confirmed").checked,
    providers: [$("settings-web-chatgpt").checked ? "chatgpt" : null, $("settings-web-claude").checked ? "claude" : null,
                $("settings-web-gemini").checked ? "gemini" : null].filter(Boolean)};
}

async function loadWebAdvisorJobs() {
  if (!settingsDraft || !state.session) return;
  const draft = settingsDraft, mode = state.mode, sid = state.session;
  const path = `/api/sessions/${encodeURIComponent(sid)}/web-advisors`;
  $("web-advisor-feedback").textContent = "读取当前会话的网页顾问任务…";
  $("web-advisor-detail").hidden = true;
  try {
    const jobs = await get(path, {mode});
    if (settingsDraft !== draft) return;
    $("web-advisor-jobs").replaceChildren();
    for (const job of jobs) {
      const button = text("button", `${job.provider} · ${webAdvisorStatuses[job.status] || job.status}\n${new Date(job.created * 1000).toLocaleString()}`, "knowledge-card");
      button.type = "button";
      button.onclick = async () => {
        try {
          const detail = await get(`${path}/${encodeURIComponent(job.job_id)}`, {mode});
          if (settingsDraft !== draft) return;
          $("web-advisor-detail").hidden = false;
          $("web-advisor-detail").open = true;
          $("web-advisor-report").textContent = `${detail.job_id}\n${webAdvisorStatuses[detail.status] || detail.status}\n网页输出未核验，不能作为执行指令。\n\n` +
            JSON.stringify({report: detail.report, sources: detail.payload.sources, approved_text_sha256: detail.payload.body_sha256}, null, 2);
          $("web-advisor-actions").replaceChildren();
          const stale = ["connecting", "sending", "sent"].includes(detail.status) && Date.now() / 1000 - detail.updated > detail.payload.config.timeout + 30;
          if (detail.status === "uncertain" || stale) {
            const resolve = text("button", "我已核实网页，解除队列（不会重发）", "small-button");
            resolve.type = "button";
            resolve.onclick = async () => {
              if (!confirm("请先在专用浏览器核实该任务是否已经发送或完成。解除队列不会重发，也不表示原任务成功。确认已核实？")) return;
              resolve.disabled = true;
              try {
                const response = await fetch(`${path}/${encodeURIComponent(job.job_id)}/resolve?${new URLSearchParams({mode})}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({verified:true})});
                if (!response.ok) throw new Error(await response.text());
                if (settingsDraft !== draft) return;
                await loadWebAdvisorJobs();
                $("web-advisor-feedback").textContent = "队列已解除，没有重发网页请求。";
              } catch (error) { if (settingsDraft === draft) $("web-advisor-feedback").textContent = error.message; }
              finally { resolve.disabled = false; }
            };
            $("web-advisor-actions").append(resolve);
          }
        } catch (error) { if (settingsDraft === draft) $("web-advisor-feedback").textContent = error.message; }
      };
      $("web-advisor-jobs").append(button);
    }
    $("web-advisor-feedback").textContent = jobs.length ? `${jobs.length} 个任务；结果未知的请求不能自动重发。` : "暂无网页顾问任务。在 Code/Agent 中请求 ChatGPT、Claude 或 Gemini 顾问后，会先展示外发审批。";
  } catch (error) { if (settingsDraft === draft) $("web-advisor-feedback").textContent = error.message; }
}

function updateEndpointPreview() {
  const label = $("settings-endpoint-preview");
  const paths = {chat_completions: "/chat/completions", responses: "/responses"};
  label.classList.remove("error");
  try {
    const url = new URL($("settings-url").value.trim() || "https://api.openai.com/v1");
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error();
    let path = url.pathname.replace(/\/+$/, "");
    const detected = Object.keys(paths).find(type => path.toLowerCase().endsWith(paths[type]));
    const selected = $("settings-api-type").value || "chat_completions";
    if (detected && selected !== "auto" && selected !== detected) {
      label.textContent = "完整地址与所选接口不一致，请切换接口类型或填写基础 URL。";
      label.classList.add("error");
      return;
    }
    if (detected) path = path.slice(0, -paths[detected].length);
    const type = selected === "auto" ? detected || "chat_completions" : selected;
    label.textContent = `请求地址：${url.origin}${path}${paths[type]}`;
  } catch {
    label.textContent = "请输入有效的 http/https 基础 URL，不能包含密码或查询参数。";
    label.classList.add("error");
  }
}

function captureSettingsModel() {
  const model = settingsDraft?.selected;
  if (!model) return;
  Object.assign(model, {
    id: $("settings-model-id").value.trim(), label: $("settings-label").value.trim(),
    model: $("settings-model").value.trim(), base_url: $("settings-url").value.trim(),
    api_type: $("settings-api-type").value,
    api_key: $("settings-key").value, no_key: $("settings-no-key").checked,
    context_window: $("settings-context").value ? Number($("settings-context").value) : null,
    stream_usage: $("settings-stream-usage").value === "auto" ? null : $("settings-stream-usage").value === "true",
  });
}

function drawSettingsModels() {
  $("settings-model-list").replaceChildren();
  for (const model of settingsDraft?.models || []) {
    const button = text("button", "");
    button.type = "button";
    button.setAttribute("aria-current", String(model === settingsDraft.selected));
    button.append(text("span", model.label || model.id || "新模型"), text("span", model.model || "待填写模型 ID", "model-caption"));
    button.onclick = () => {
      captureSettingsModel();
      settingsDraft.selected = model;
      showSettingsModel();
      drawSettingsModels();
    };
    $("settings-model-list").append(button);
  }
  $("settings-add").disabled = settingsSaving || (settingsDraft?.models.length || 0) >= 50;
}

function showSettingsModel() {
  const model = settingsDraft?.selected;
  $("settings-editor").hidden = !model;
  $("settings-model-empty").hidden = Boolean(model);
  for (const field of $("settings-editor").querySelectorAll("input,select,button")) field.disabled = !model || settingsSaving;
  if (!model) return;
  $("settings-model-id").value = model.id;
  $("settings-model-id").disabled = model.source !== "new" || settingsSaving;
  $("settings-label").value = model.label || "";
  $("settings-model").value = model.model || "";
  $("settings-url").value = model.base_url || "";
  $("settings-api-type").value = model.api_type || "chat_completions";
  updateEndpointPreview();
  $("settings-key").value = model.api_key || "";
  $("settings-key").placeholder = model.has_key ? "已配置，留空保留原 Key" : "输入 API Key";
  $("settings-no-key").checked = Boolean(model.no_key);
  $("settings-key").disabled = Boolean(model.no_key) || settingsSaving;
  $("settings-key-hint").textContent = model.no_key ? "仅用于不需要鉴权的本地或私有服务。" : model.has_key
    ? "已配置 Key，原值不会返回到浏览器；填写新值可以替换。" : "新模型留空可沿用默认档案的 Key，也可勾选下方的无需 Key。";
  $("settings-context").value = model.context_window ?? "";
  $("settings-stream-usage").value = model.stream_usage == null ? "auto" : String(model.stream_usage);
  $("settings-source").textContent = model.source === "env" ? "来自 .env，保存后使用本地设置覆盖，原文件保持不变。"
    : model.source === "new" ? "新档案，保存后即可在模型列表中选择。" : "已保存的本地模型档案。";
}

async function openSettings() {
  if ($("settings-dialog").open) return;
  settingsDraft = null;
  $("settings-form").hidden = true;
  $("settings-dialog").showModal();
  try {
    const data = await get("/api/settings");
    if (!$("settings-dialog").open) return;
    settingsDraft = {...data, selected: data.models.find(model => model.id === $("model").value) || data.models[0] || null};
    $("settings-port").value = data.port;
    showWebAdvisorSettings(data.web_advisor);
    $("web-advisor-jobs").replaceChildren();
    $("web-advisor-feedback").textContent = "任务按当前工作台会话隔离；点击上方按钮查看。";
    $("web-advisor-detail").hidden = true;
    $("settings-port-hint").textContent = `当前端口 ${data.current_port}；修改后在下次启动时生效，当前会话不会中断。`;
    showSettingsModel();
    drawSettingsModels();
    settingsFeedback(state.busy ? "当前任务尚未结束，暂时不能保存设置。" : "Key 不会回显；留空保留现有 Key。模型保存后下一次请求即可使用。", state.busy);
  } catch (error) {
    showSettingsModel();
    settingsFeedback(`无法读取设置：${error.message}`, true);
  } finally {
    $("settings-form").hidden = false;
    controls();
  }
}

async function saveSettings(event) {
  event.preventDefault();
  if (!settingsDraft || settingsSaving || state.busy || state.loading) return;
  captureSettingsModel();
  const incomplete = settingsDraft.models.find(model => !model.id || !model.model);
  if (incomplete) {
    settingsDraft.selected = incomplete;
    showSettingsModel();
    drawSettingsModels();
    $("settings-form").reportValidity();
    return;
  }
  if (!$("settings-form").reportValidity()) return;
  settingsSaving = true;
  for (const field of $("settings-dialog").querySelectorAll("input,select,button")) field.disabled = true;
  settingsFeedback("正在保存设置…");
  try {
    const selectedId = settingsDraft.selected?.id.toLowerCase();
    const payload = {revision: settingsDraft.revision, port: Number($("settings-port").value), web_advisor: captureWebAdvisorSettings(), models: settingsDraft.models.map(model => ({
      id: model.id, label: model.label, model: model.model, base_url: model.base_url, api_key: model.api_key || "",
      api_type: model.api_type || "chat_completions",
      no_key: Boolean(model.no_key), context_window: model.context_window ?? null, stream_usage: model.stream_usage ?? null,
    }))};
    const response = await fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    if (!response.ok) throw new Error(await response.text());
    const saved = await response.json();
    settingsDraft = {...saved, selected: saved.models.find(model => model.id === selectedId) || saved.models[0] || null};
    showWebAdvisorSettings(saved.web_advisor);
    await loadState();
    settingsFeedback(saved.restart_required ? `已保存。模型立即可用；端口 ${saved.port} 将在停止旧服务并重新启动后生效。`
      : "设置已保存，下一次请求将使用新的模型配置。Key 输入已清空。" );
  } catch (error) {
    settingsFeedback(error.message, true);
  } finally {
    settingsSaving = false;
    for (const field of $("settings-dialog").querySelectorAll("input,select,button")) field.disabled = false;
    showSettingsModel();
    drawSettingsModels();
    updateWebAdvisorBrowserFields();
    controls();
  }
}

function showPanel(visible) {
  $("panel").hidden = !visible;
  app.classList.toggle("panel-closed", !visible);
  $("panel-toggle").setAttribute("aria-expanded", String(visible));
  if (!visible) stopWebLive();
  else if (!$("web-live").hidden && webLive.job) pollWebLive(webLive.epoch);
}

function expandViewer(expanded) {
  app.classList.toggle("viewer-expanded", expanded);
  const button = $("viewer-expand");
  const label = expanded ? "还原预览" : "放大预览";
  button.replaceChildren(icon(expanded ? "collapse" : "expand"));
  button.title = label;
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-pressed", String(expanded));
}

function showViewer(visible) {
  if (!visible) expandViewer(false);
  $("viewer").hidden = !visible;
  app.classList.toggle("viewer-open", visible);
  if (visible && !desktopViewer.matches) closeRail();
}

function bindSplitter(name, panel, direction) {
  const handle = $(`split-${name}`);
  const property = `--${name}-width`;
  const key = `astro-layout-${name}`;
  let start = null;
  const width = () => Math.round(panel.getBoundingClientRect().width);
  const applyWidth = (value) => app.style.setProperty(property, `${Math.max(1, Math.round(value))}px`);
  const saveWidth = () => {
    applyWidth(width());
    localStorage.setItem(key, String(width()));
  };
  const saved = Number(localStorage.getItem(key));
  if (saved > 0 && Number.isFinite(saved)) applyWidth(saved);
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || start) return;
    handle.focus({preventScroll: true});
    handle.setPointerCapture(event.pointerId);
    start = {x: event.clientX, width: width(), pointer: event.pointerId};
    handle.classList.add("dragging");
    app.classList.add("resizing");
  });
  handle.addEventListener("pointermove", (event) => {
    if (event.pointerId === start?.pointer) applyWidth(start.width + direction * (event.clientX - start.x));
  });
  handle.addEventListener("lostpointercapture", (event) => {
    if (event.pointerId !== start?.pointer) return;
    if (width() > 0) saveWidth();
    start = null;
    handle.classList.remove("dragging");
    app.classList.remove("resizing");
  });
  handle.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const step = event.shiftKey ? 32 : 12;
    applyWidth(width() + direction * (event.key === "ArrowRight" ? step : -step));
    saveWidth();
  });
  handle.addEventListener("dblclick", () => {
    app.style.removeProperty(property);
    localStorage.removeItem(key);
  });
}

function closeRail() {
  app.classList.remove("nav-open");
  $("sidebar-toggle").setAttribute("aria-expanded", "false");
}

function setupWelcome() {
  const chat = state.mode === "chat";
  $("nav-chat").setAttribute("aria-pressed", String(chat));
  $("nav-task").setAttribute("aria-pressed", String(!chat));
  $("mode").hidden = chat;
  $("welcome-title").textContent = chat ? "开始一段对话" : "今天想研究什么？";
  $("welcome-text").textContent = chat ? "问一个天文问题，或从工作区附加文件一起分析。" : "让 Agent 检索资料、分析数据或处理工作区文件。";
  $("activity-empty").textContent = chat ? "发送消息后，这里会显示本轮进度。" : "提交任务后，这里会显示执行进度和审批请求。";
  $("prompt").placeholder = chat ? "输入你的问题…" : "描述你要完成的任务…";
  $("suggestions").replaceChildren();
  for (const suggestion of presets[state.mode]) {
    const button = text("button", suggestion);
    button.onclick = () => {
      $("prompt").value = suggestion;
      controls();
      $("prompt").focus();
    };
    $("suggestions").append(button);
  }
}

function resetView(sid = null) {
  stopWebLive(true);
  state.session = sid;
  state.pending = null;
  showUsage();
  state.attachments = [];
  $("messages").replaceChildren();
  $("events").replaceChildren();
  $("activity-empty").hidden = false;
  $("welcome").hidden = Boolean(sid);
  $("timeline").scrollTop = 0;
  $("prompt").value = "";
  status();
  closeRail();
  drawAttachments();
  drawSessions();
}

async function changeMode(mode) {
  if (state.busy || state.loading) return;
  const view = ++state.view;
  state.mode = mode;
  localStorage.setItem("astro-mode", mode);
  state.sessions = [];
  state.loading = true;
  resetView();
  setupWelcome();
  try {
    await loadState(view);
  } catch (error) {
    status(error.message);
  } finally {
    state.loading = false;
    controls();
  }
  const saved = localStorage.getItem(`astro-session-${mode}`);
  const chosen = state.sessions.find(item => item.id === saved) || state.sessions.find(item => item.pending) || state.sessions[0];
  if (view === state.view && chosen) await openSession(chosen.id);
}

async function openSession(id) {
  if (state.busy || state.loading) return;
  const view = ++state.view;
  state.loading = true;
  resetView(id);
  try {
    const history = await get(`/api/sessions/${encodeURIComponent(id)}`, {mode: state.mode});
    if (view !== state.view) return;
    for (const message of history) appendMessage(message.role, message.html ?? message.text, Boolean(message.html));
    $("welcome").hidden = history.length > 0;
    localStorage.setItem(`astro-session-${state.mode}`, id);
    await loadPending(view);
    scrollBottom();
  } catch (error) {
    resetView();
    status(error.message);
  } finally {
    state.loading = false;
    controls();
  }
}

async function loadPending(view = state.view) {
  if (!state.session || state.mode === "chat") return;
  const pending = await get(`/api/sessions/${encodeURIComponent(state.session)}/pending`, {mode: state.mode});
  if (view !== state.view) return;
  state.pending = pending;
  if (!pending) return;
  const old = $("events").querySelector(".resume-card");
  if (old) old.remove();
  const card = activity(pending.status === "waiting" ? "这轮任务正在等待你的审批或回答。" : "这轮任务尚未完成，执行状态已保存。", "approval resume-card");
  if (pending.requests?.length) card.append(text("p", `有 ${pending.requests.length} 个待处理请求。`));
  const actions = text("div", "", "approval-actions");
  if (pending.status === "waiting") {
    const resume = text("button", "继续处理", "small-button");
    resume.onclick = () => startTurn({mode: state.mode, profile: pending.profile, session: state.session,
                                    text: "", attachments: [], resume: true});
    actions.append(resume);
  }
  const discard = text("button", "放弃本轮", "small-button");
  discard.onclick = async () => {
    if (state.busy) return;
    discard.disabled = true;
    try {
      const response = await fetch(`/api/sessions/${encodeURIComponent(state.session)}/discard?mode=${encodeURIComponent(state.mode)}`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({run_id: pending.run_id}),
      });
      if (!response.ok) throw new Error(await response.text());
      state.pending = null;
      card.replaceChildren(text("span", "本轮已放弃，历史记录保留。"));
      await loadState();
      controls();
    } catch (error) {
      status(error.message);
      discard.disabled = false;
    }
  };
  actions.append(discard);
  card.append(actions);
  controls();
}

function showTab(tab) {
  for (const name of ["activity", "files", "web"]) {
    $(name === "web" ? "web-live" : name).hidden = name !== tab;
    $(`${name}-tab`).setAttribute("aria-pressed", String(name === tab));
  }
  if (tab === "files") loadFiles();
  if (tab === "web") {
    refreshWebLive();
    if (webLive.job) pollWebLive(webLive.epoch);
  } else stopWebLive(true);
}

function activity(message, kind = "") {
  $("activity-empty").hidden = true;
  const event = text("div", message, `event ${kind}`);
  $("events").append(event);
  return event;
}

function interaction(event, socket, cards) {
  expandViewer(false);
  if (!desktopViewer.matches) showViewer(false);
  showPanel(true);
  showTab("activity");
  status(event.type === "approval" ? "等待逐项审批" : "等待你的回答");
  cards.get(event.id)?.card.remove();
  const card = activity(event.type === "approval" ? "请逐项审核操作" : event.question || "请提供继续任务所需的信息", "approval interaction");
  const error = text("p", "", "decision-error");
  error.setAttribute("role", "alert");
  const feedback = text("p", "", "muted");
  const setDisabled = disabled => {
    for (const input of card.querySelectorAll("button,input,textarea,select")) input.disabled = disabled;
  };
  const submit = value => {
    if (socket.readyState !== WebSocket.OPEN) return;
    error.textContent = "";
    socket.send(JSON.stringify({kind: "decision", id: event.id, value}));
    setDisabled(true);
    feedback.textContent = "正在提交…";
  };
  cards.set(event.id, {card, error, feedback, setDisabled});
  if (event.type === "approval") {
    const decisions = new Array(event.actions.length).fill(null);
    let submitAll;
    for (const [index, action] of event.actions.entries()) {
      const section = text("section", "", "approval-action");
      const title = ({write_file:"写入文件",edit_file:"修改文件",run_powershell:"PowerShell 命令",run_bash:"Bash 命令"})[action.name] || action.name;
      section.append(text("h3", `${index + 1}. ${title}`), text("pre", action.command || action.description || JSON.stringify(action.args, null, 2)));
      const edits = text("details", "", "argument-editor");
      edits.append(text("summary", "修改参数"));
      const fields = [];
      const editValues = {...action.args};
      if (action.name === "edit_file" && !("replace_all" in editValues)) editValues.replace_all = false;
      for (const [name, value] of Object.entries(editValues)) {
        const label = text("label", ({command:"命令",file_path:"文件路径",content:"文件内容",old_string:"原内容",new_string:"替换内容",replace_all:"替换所有匹配"})[name] || name);
        const input = document.createElement(typeof value === "boolean" ? "input" : "textarea");
        if (typeof value === "boolean") { input.type = "checkbox"; input.checked = value; }
        else { input.rows = name === "content" ? 6 : 3; input.value = typeof value === "string" ? value : JSON.stringify(value); }
        label.append(input);
        edits.append(label);
        fields.push([name, value, input]);
      }
      section.append(edits);
      const reason = document.createElement("textarea");
      reason.rows = 2;
      reason.placeholder = "拒绝原因（可选）";
      reason.setAttribute("aria-label", "拒绝原因");
      section.append(reason);
      const selection = text("p", "尚未选择", "muted");
      const buttons = text("div", "", "approval-actions");
      for (const choice of action.allowed_decisions || ["approve", "edit", "reject"]) {
        const button = text("button", ({approve:"批准执行",edit:"修改后执行",reject:"拒绝",respond:"回复结果"})[choice] || choice, "small-button");
        button.onclick = () => {
          try {
            let decision = {type: choice};
            if (choice === "edit") {
              const args = Object.fromEntries(fields.map(([name, original, input]) => [name,
                typeof original === "boolean" ? input.checked : typeof original === "string" ? input.value : JSON.parse(input.value)]));
              decision.edited_action = {name: action.name, args};
            }
            if (choice === "reject" || choice === "respond") decision.message = reason.value.trim() || (choice === "reject" ? "用户拒绝执行；不要重复提交此操作。" : "");
            decisions[index] = decision;
            selection.textContent = `已选择：${button.textContent}`;
            error.textContent = "";
            if (decisions.length === 1) submit(event.protocol === "per_call" ? decision : {decisions});
            else if (submitAll) submitAll.disabled = decisions.some(item => !item);
          } catch { error.textContent = "修改后的参数不是有效 JSON。"; }
        };
        buttons.append(button);
      }
      section.append(selection, buttons);
      card.append(section);
    }
    if (decisions.length > 1) {
      submitAll = text("button", "提交全部决策", "small-button");
      submitAll.disabled = true;
      submitAll.onclick = () => submit({decisions});
      card.append(submitAll);
    }
  } else {
    if (event.type === "custom") {
      card.append(text("pre", JSON.stringify(event.raw, null, 2)));
      if (event.response_schema) {
        const format = text("details", "", "argument-editor");
        format.append(text("summary", "回复格式"), text("pre", JSON.stringify(event.response_schema, null, 2)));
        card.append(format);
      }
    }
    const answer = document.createElement("textarea");
    answer.rows = 3;
    answer.placeholder = event.type === "custom" ? "填写 JSON 回复" : "填写回答或拒绝原因";
    answer.setAttribute("aria-label", "回复内容");
    const actions = text("div", "", "approval-actions");
    if (event.type === "confirmation") {
      for (const [label, approved] of [["批准",true],["拒绝",false]]) {
        const button = text("button", label, "small-button");
        button.onclick = () => submit({approved, reason: answer.value});
        actions.append(button);
      }
    } else {
      for (const option of event.options || []) {
        const button = text("button", option, "small-button");
        button.onclick = () => submit({answer: option});
        actions.append(button);
      }
      const button = text("button", "回答并继续", "small-button");
      button.onclick = () => {
        try { submit(event.type === "custom" ? JSON.parse(answer.value) : {answer: answer.value}); }
        catch { error.textContent = "请输入有效的 JSON 回复。"; }
      };
      actions.append(button);
    }
    card.append(answer, actions);
  }
  card.append(error, feedback);
  card.scrollIntoView({block: "nearest"});
}

function send(event) {
  event.preventDefault();
  const question = $("prompt").value.trim();
  if (state.busy) {
    if (state.stopping || liveSocket?.readyState !== WebSocket.OPEN || !question) return;
    const id = crypto.randomUUID();
    const element = appendMessage("user supplement-message", `[补充提交中]\n${question}`);
    supplementCards.set(id, {element, text:question, applied:false});
    liveSocket.send(JSON.stringify({kind:"steer", id, text:question}));
    $("prompt").value = "";
    controls();
    scrollBottom();
    return;
  }
  if (state.loading || state.pending || !question || !$("model").value) return;
  const compacting = /^\/compact(?:\s|$)/i.test(question);
  const payload = {mode: state.mode, profile: $("model").value, session: state.session, text: question, attachments: compacting ? [] : [...state.attachments]};
  if (!compacting && selectedSkills.length) payload.skills = selectedSkills.map(entry => ({...entry}));
  if (!compacting) {selectedSkills = []; drawSkillSelection();}
  if ($("model").selectedOptions[0]?.dataset.qwen === "true") {
    payload.reasoning_effort = compacting ? "none" : $("qwen-reasoning").value;
    $("qwen-reasoning").value = "none";
  }
  startTurn(payload);
}

function startTurn(payload) {
  if (state.busy || state.loading) return;
  const {text: question} = payload;
  const compacting = /^\/compact(?:\s|$)/i.test(question);
  const socket = new WebSocket(`ws://${location.host}/api/run`);
  liveSocket = socket;
  state.stopping = false;
  stopAndContinue = null;
  supplementCards = new Map();
  const cards = new Map();
  state.busy = true;
  state.pending = null;
  $("events").querySelector(".resume-card")?.remove();
  showUsage();
  if (!payload.resume) $("prompt").value = "";
  if (!compacting && !payload.resume) state.attachments = [];
  drawAttachments();
  const label = payload.attachments.length ? `${question}\n\n附件：${payload.attachments.join("、")}` : question;
  if (!payload.resume) appendMessage(compacting ? "notice" : "user", label);
  scrollBottom();
  controls();
  status("连接中…");
  let response = null;
  let draft = null;
  let draftBody = null;
  let finished = false;
  let failed = false;
  let stopped = false;
  const restoreSupplements = items => {
    const pending = [];
    for (const item of items) {
      const entry = supplementCards.get(item.id);
      if (!entry || entry.applied || entry.restored) continue;
      entry.restored = true;
      entry.element.textContent = `[补充尚未应用，已保留在输入框]\n${entry.text}`;
      pending.push(entry.text);
    }
    if (pending.length) $("prompt").value = [pending.join("\n\n"), $("prompt").value].filter(Boolean).join("\n\n");
  };
  let parts = [];
  let frame = 0;

  function flush() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    if (!parts.length) return;
    const follow = atBottom();
    if (!response) response = appendMessage("assistant live");
    response.append(document.createTextNode(parts.join("")));
    parts = [];
    if (follow) scrollBottom();
  }

  socket.onopen = () => {
    socket.send(JSON.stringify(payload));
    status("处理中…");
    activity(payload.resume ? "正在恢复原任务" : "已提交");
    controls();
  };
  socket.onmessage = ({data}) => {
    const item = JSON.parse(data);
    if (item.kind === "stop_requested") { state.stopping = true; status(item.text); controls(); }
    if (item.kind === "stopped") {
      stopped = true;
      finished = true;
      flush();
      if (response) response.classList.remove("live");
      restoreSupplements(item.pending || []);
      appendMessage("notice", item.text);
      activity("已停止；不自动撤销已执行操作");
    }
    if (item.kind === "steer_unapplied") restoreSupplements(item.items || []);
    if (item.kind === "steer_accepted" || item.kind === "steer_applied") {
      const entry = supplementCards.get(item.id);
      if (entry) {
        entry.applied ||= item.kind === "steer_applied";
        entry.element.textContent = `${entry.applied ? "[补充已进入任务上下文]" : "[补充已接收，等待安全步骤]"}\n${entry.text}`;
      }
      if (item.kind === "steer_applied") {
        flush();
        if (response) {response.classList.remove("live");response = null;}
        if (draft) {draft.open = false;draft = null;draftBody = null;}
        activity("补充指令已进入当前任务；重新规划未执行的操作");
      }
    }
    if (item.kind === "steer_error") {
      const entry = supplementCards.get(item.id);
      if (entry && !entry.applied) {
        restoreSupplements([{id:item.id}]);
        entry.element.textContent = `[补充未提交：${item.text}]\n${entry.text}`;
      }
      controls();
    }
    if (item.kind === "decision_invalidated") {
      for (const id of item.ids) {
        const entry = cards.get(id);
        if (entry) {entry.setDisabled(true);entry.feedback.textContent = "旧请求已取消，未批准操作";}
      }
      activity(item.text);
    }
    if (item.kind === "status") status(item.text);
    if (item.kind === "compact") {
      appendMessage("notice", item.html, true);
      activity(item.changed ? "上下文已压缩" : item.text);
      finished = true;
      scrollBottom();
    }
    if (item.kind === "session") {
      state.session = item.id;
      localStorage.setItem(`astro-session-${state.mode}`, item.id);
    }
    if (item.kind === "usage") showUsage(item);
    if (item.kind === "draft") {
      const follow = atBottom();
      if (!draft) {
        draft = text("details", "", "draft");
        draft.open = true;
        draftBody = text("pre", "");
        draft.append(text("summary", "内部草稿"), draftBody);
        $("messages").insertBefore(draft, response);
      }
      draftBody.append(document.createTextNode(item.text));
      if (follow) scrollBottom();
      status("生成草稿…");
    }
    if (item.kind === "delta") {
      parts.push(item.text);
      if (!frame) frame = requestAnimationFrame(flush);
      status("生成回答…");
    }
    if (item.kind === "approval" || item.kind === "question") interaction(item, socket, cards);
    if (item.kind === "decision_error") {
      const entry = cards.get(item.id);
      if (entry) { entry.error.textContent = item.text; entry.feedback.textContent = ""; entry.setDisabled(false); }
    }
    if (item.kind === "decision_saved") {
      const entry = cards.get(item.id);
      if (entry) { entry.feedback.textContent = "已提交"; entry.setDisabled(true); }
    }
    if (item.kind === "tool") activity(`${item.status === "error" ? "工具出错" : "工具完成"} · ${item.name}`);
    if (item.kind === "skill") activity(`技能已完整加载 · ${item.name}\n${item.checksum}`);
    if (item.kind === "experiment") {
      const entry = activity(`已冻结实验计划 · ${item.title}`);
      const button = text("button", "查看实验", "small-button");
      button.type = "button";
      button.onclick = () => openExperiments(item.id);
      entry.append(button);
    }
    if (item.kind === "web_advisor") {
      if (item.phase === "answer") {
        if (webLive.job === item.job_id && webLiveVisible()) $("web-live-answer").textContent = item.answer;
      } else {
      const phase = {prepared:"冻结文本 / 等待外发审批", queued:"排队", connecting:"连接专用浏览器", opened:"已建立独立标签页", location:"已记录网页会话位置", sending:"发送批准文本", sent:"等待网页回复", finished:"结束", cached:"复用记录，未重发"}[item.phase] || item.phase;
      activity(`网页顾问 · ${item.provider} · ${phase}${item.status ? " · " + (webAdvisorStatuses[item.status] || item.status) : ""}\n${item.job_id}`);
        if (webLiveVisible()) refreshWebLive(webLive.job || item.job_id);
      }
    }
    if (item.kind === "retrieval") activity(`科研记忆 · ${item.engine} · ${item.references.length} 条来源 · ${item.knowledge_count} 条知识\n${item.references.join("\n")}${item.warnings.length ? "\n" + item.warnings.join("；") : ""}`);
    if (item.kind === "subagent") {
      const phase = {started: "开始", model: "分析", tool: "查询", finished: "结束", cached: "复用已保存结果"}[item.phase] || item.phase;
      activity(`子 Agent · ${item.agent_kind === "literature" ? "文献检索" : "证据核验"} · ${phase}${item.tool ? " · " + item.tool : ""}${item.status ? " · " + item.status : ""}\n${item.job_id}`);
    }
    if (item.kind === "done") {
      const follow = atBottom();
      flush();
      for (const reply of item.user_replies || []) {
        const note = appendMessage("user", reply);
        if (response) response.before(note);
      }
      if (!response) response = appendMessage("assistant");
      response.classList.remove("live");
      response.innerHTML = item.html;
      if (draft) draft.open = false;
      if (item.thinking?.reasoning) {
        const details = text("details", "", "draft");
        details.append(text("summary", "思考过程"), text("pre", item.thinking.reasoning));
        response.before(details);
      }
      activity("已完成");
      finished = true;
      if (follow) scrollBottom();
    }
    if (item.kind === "error") {
      const follow = atBottom();
      flush();
      failed = true;
      appendMessage("error", item.text);
      activity("处理失败", "error");
      if (follow) scrollBottom();
    }
  };
  socket.onclose = async () => {
    flush();
    if (!finished && !failed) {
      const follow = atBottom();
      appendMessage("error", "工作台连接已断开");
      if (follow) scrollBottom();
    }
    for (const entry of cards.values()) entry.setDisabled(true);
    try {
      await loadState();
      await loadPending();
      status(stopped ? "已停止" : finished ? "" : "本轮未完成");
    } catch (error) {
      status(error.message);
    } finally {
      state.busy = false;
      state.stopping = false;
      if (liveSocket === socket) liveSocket = null;
      controls();
      checkEngine();
    }
    const next = stopAndContinue;
    stopAndContinue = null;
    if (next && stopped && !state.pending) {
      const pending = $("prompt").value.trim();
      const text = [pending, next.text].filter(Boolean).join("\n\n");
      if (text.length > 48000) {
        $("prompt").value = text;
        status("合并补充超过 48000 字符，请精简后发送；未自动提交");
        controls();
      } else startTurn({mode:payload.mode, profile:payload.profile, session:state.session, text, attachments:[]});
    } else if (next) {
      $("prompt").value = [$("prompt").value, next.text].filter(Boolean).join("\n\n");
      status("未确认停止或任务仍待处理，补充已留在输入框；不会自动重发");
      controls();
    }
  };
}

async function loadFiles(folder = state.folder) {
  const view = ++state.fileView;
  state.folder = folder;
  state.file = null;
  $("folder").textContent = `/${folder}`;
  $("folder").title = `/${folder}`;
  $("up").disabled = !folder;
  $("file-list").replaceChildren();
  showViewer(false);
  controls();
  try {
    const entries = await get("/api/files", {path: folder});
    if (view !== state.fileView) return;
    for (const entry of entries) {
      const button = text("button", "", "file");
      button.title = entry.name;
      const kind = fileIcon(entry);
      button.append(icon(kind, `file-icon ${kind}`), text("span", entry.name, "label"));
      button.onclick = () => entry.folder ? loadFiles(entry.path) : preview(entry, button);
      $("file-list").append(button);
    }
    if (!entries.length) $("file-list").append(text("p", "这个目录没有可显示的文件", "muted"));
  } catch (error) {
    if (view === state.fileView) $("file-list").replaceChildren(text("p", error.message, "muted"));
  }
}

async function preview(entry, button) {
  const view = ++state.fileView;
  state.file = null;
  controls();
  for (const row of $("file-list").querySelectorAll("button")) row.removeAttribute("aria-current");
  button.setAttribute("aria-current", "true");
  showViewer(true);
  $("filename").textContent = entry.name;
  $("filename").title = entry.path;
  $("file-text").hidden = true;
  $("preview-status").textContent = "加载中…";
  try {
    const data = await get("/api/preview", {path: entry.path});
    if (view !== state.fileView) return;
    state.file = {...entry, attach: data.attach};
    if (data.html) $("file-text").innerHTML = data.html;
    else $("file-text").textContent = data.text || "";
    $("file-text").hidden = data.text === null;
    $("preview-status").textContent = data.text === null ? "此格式暂无文本预览" : (data.truncated ? "仅显示文件的前 64 KiB" : "");
    $("preview-status").hidden = !$("preview-status").textContent;
    controls();
  } catch (error) {
    if (view === state.fileView) $("preview-status").textContent = error.message;
  }
}

function drawAttachments() {
  $("attachments").hidden = state.attachments.length === 0;
  $("attachments").replaceChildren();
  for (const path of state.attachments) {
    const button = text("button", `${path.split("/").at(-1)} ×`, "small-button");
    button.type = "button";
    button.title = `移除 ${path}`;
    button.onclick = () => {
      state.attachments = state.attachments.filter((item) => item !== path);
      drawAttachments();
    };
    $("attachments").append(button);
  }
  controls();
}

$("settings-open").onclick = openSettings;
$("memory-open").onclick = openMemory;
$("memory-close").onclick = () => $("memory-dialog").close();
$("memory-dialog").addEventListener("cancel", event => { if (memorySaving || knowledgeSaving) event.preventDefault(); });
$("memory-dialog").addEventListener("close", () => { memoryDraft = null; });
$("memory-save").onclick = () => changeMemory("edit");
$("memory-restore").onclick = () => changeMemory("restore");
$("memory-reset").onclick = () => changeMemory("restore", true);
$("memory-prev").onclick = () => selectMemory(memoryDraft.selected, Math.max(0, memoryDraft.offset - 50));
$("memory-next").onclick = () => selectMemory(memoryDraft.selected, memoryDraft.offset + 50);
$("knowledge-search-form").onsubmit = searchKnowledge;
$("subagent-refresh").onclick = loadSubagents;
$("knowledge-new").onclick = () => editKnowledge();
$("knowledge-form").onsubmit = event => {
  event.preventDefault();
  const entry = memoryDraft?.knowledgeEdit;
  updateKnowledge({action: "save", entry_id: $("knowledge-entry-id").value || undefined,
    revision: $("knowledge-revision").value ? Number($("knowledge-revision").value) : undefined,
    title: $("knowledge-title").value, keywords: $("knowledge-keywords").value.split(/[,，;；\n]/).map(value => value.trim()).filter(Boolean),
    source_reference: $("knowledge-reference").value.trim(), source_mode: entry?.source_mode || memoryDraft.mode,
    source_session: entry?.source_session || memoryDraft.sid, quote: $("knowledge-quote").value, note: $("knowledge-note").value,
    priority: Number($("knowledge-priority").value), scope: $("knowledge-shared").checked ? "project" : "session", enabled: entry?.enabled ?? true});
};
$("knowledge-reindex").onclick = async () => {
  if (!memoryDraft || knowledgeSaving || state.pending || state.busy) return;
  knowledgeSaving = true;
  memoryControls();
  try {
    const response = await fetch(`${memoryPath("/reindex")}?${new URLSearchParams({mode: memoryDraft.mode})}`, {method: "POST"});
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    knowledgeFeedback(`已重建 ${data.chunks} 个检索片段，原文和摘要版本未改动。`);
  } catch (error) { knowledgeFeedback(error.message, true); }
  finally { knowledgeSaving = false; memoryControls(); }
};
$("settings-nav").onclick = openSettings;
$("settings-close").onclick = () => $("settings-dialog").close();
$("settings-dialog").addEventListener("cancel", event => { if (settingsSaving) event.preventDefault(); });
$("settings-dialog").addEventListener("close", () => { settingsDraft = null; $("settings-key").value = ""; });
$("settings-form").onsubmit = saveSettings;
$("web-advisor-refresh").onclick = loadWebAdvisorJobs;
$("settings-web-browser").onchange = () => updateWebAdvisorBrowserFields(true);
$("settings-web-proxy-mode").onchange = () => updateWebAdvisorBrowserFields(true);
$("settings-web-proxy-url").oninput = () => { $("settings-web-fingerprint-confirmed").checked = false; };
$("settings-web-cdp").oninput = () => { $("settings-web-fingerprint-confirmed").checked = false; };
for (const field of $("settings-editor").querySelectorAll("input,select")) {
  field.addEventListener("input", () => {
    captureSettingsModel();
    updateEndpointPreview();
    $("settings-key").disabled = $("settings-no-key").checked;
    drawSettingsModels();
  });
}
$("settings-add").onclick = () => {
  if (!settingsDraft || settingsSaving) return;
  captureSettingsModel();
  let number = 1;
  while (settingsDraft.models.some(model => model.id.toLowerCase() === `model_${number}`)) number++;
  const model = {id: `model_${number}`, label: "", model: "", base_url: "", api_key: "", no_key: false,
                 has_key: false, source: "new", context_window: null, stream_usage: null, api_type: "chat_completions"};
  settingsDraft.models.push(model);
  settingsDraft.selected = model;
  showSettingsModel();
  drawSettingsModels();
  $("settings-model-id").focus();
};
$("settings-delete").onclick = () => {
  if (!settingsDraft || settingsSaving) return;
  settingsDraft.models = settingsDraft.models.filter(model => model !== settingsDraft.selected);
  settingsDraft.selected = settingsDraft.models[0] || null;
  showSettingsModel();
  drawSettingsModels();
  settingsFeedback("已从待保存配置中移除。点击保存后生效，关闭设置可取消这次修改。");
};

$("nav-task").onclick = () => {
  if (state.mode !== state.taskMode) changeMode(state.taskMode);
};
$("nav-chat").onclick = () => {
  if (state.mode !== "chat") changeMode("chat");
};
$("mode").onchange = () => {
  state.taskMode = $("mode").value;
  changeMode(state.taskMode);
};
$("model").onchange = () => {
  $("qwen-reasoning").value = "none";
  localStorage.setItem("astro-profile", $("model").value);
  showUsage();
  controls();
};
$("skills-open").onclick = openSkills;
$("skills-close").onclick = $("skills-done").onclick = () => $("skills-dialog").close();
$("skills-dialog").onclose = () => {skillsDraft = null;};
$("skills-refresh").onclick = async () => {try {await refreshSkills();} catch (error) {skillsFeedback(error.message, true);}};
$("skill-enabled").onclick = async () => {
  const detail = skillsDraft?.detail;
  if (!detail || skillsBusy) return;
  skillsBusy = true; skillControls();
  try {
    const response = await fetch(`/api/skills/${encodeURIComponent(detail.name)}/enabled`, {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({enabled:!detail.enabled,checksum:detail.checksum,revision:skillsDraft.revision})});
    if (!response.ok) throw new Error(await response.text());
    await refreshSkills(detail.name);
    skillsFeedback(detail.enabled ? "已禁用后续资源读取和新加载，历史与本轮已加载快照保留。" : "已启用此校验版本；内容变更后需要重新审阅。");
  } catch (error) {skillsFeedback(error.message, true);}
  finally {skillsBusy=false; skillControls(); drawSkills();}
};
$("skill-selected").onchange = () => {
  const detail = skillsDraft?.detail;
  if (!detail?.enabled) return;
  selectedSkills = selectedSkills.filter(entry => entry.name !== detail.name);
  if ($("skill-selected").checked) {
    if (selectedSkills.length>=3) {$("skill-selected").checked=false;skillsFeedback("每轮最多选择 3 个技能",true);return;}
    selectedSkills.push({name:detail.name,checksum:detail.checksum});
  }
  drawSkillSelection();
};
$("skills-official").onclick = async () => {
  if (skillsBusy) return;
  skillsBusy=true;skillControls();skillsFeedback("正在读取固定官方仓库目录…");
  try {
    const data=await get("/api/skills/official");
    $("skills-official-name").replaceChildren(...data.skills.map(name=>new Option(name,name)));
    skillsFeedback(`来源：${data.repository}。导入后仍需审阅启用。`);
  } catch (error) {skillsFeedback(error.message,true);}
  finally {skillsBusy=false;skillControls();}
};
$("skills-import-official").onclick = () => importSkill({method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({source:"anthropics",name:$("skills-official-name").value})});
$("skills-upload").onclick = () => $("skills-zip").click();
$("skills-zip").onchange = () => {
  const file=$("skills-zip").files[0];
  if (!file) return;
  if (file.size>32*1024*1024) {skillsFeedback("技能 ZIP 最大 32 MiB",true);return;}
  importSkill({method:"POST",headers:{"Content-Type":"application/zip"},body:file},$("skills-zip-name").value.trim());
  $("skills-zip").value="";
};
$("new").onclick = () => {
  localStorage.removeItem(`astro-session-${state.mode}`);
  resetView();
  $("prompt").focus();
};
$("sidebar-toggle").onclick = () => {
  const open = app.classList.toggle("nav-open");
  $("sidebar-toggle").setAttribute("aria-expanded", String(open));
  if (open && matchMedia("(max-width: 550px)").matches) {
    showViewer(false);
    showPanel(false);
  }
};
$("panel-toggle").onclick = () => {
  if (!desktopViewer.matches) showViewer(false);
  showPanel($("panel").hidden);
};
$("viewer-expand").onclick = () => expandViewer(!app.classList.contains("viewer-expanded"));
$("viewer-close").onclick = () => {
  showViewer(false);
  const selectedFile = $("file-list").querySelector('[aria-current="true"]');
  (selectedFile?.getClientRects().length ? selectedFile : $("panel-toggle")).focus();
};
$("viewer").addEventListener("keydown", (event) => {
  if (event.key === "Escape") $("viewer-close").click();
});
$("activity-tab").onclick = () => showTab("activity");
$("files-tab").onclick = () => showTab("files");
$("web-tab").onclick = () => showTab("web");
$("web-nav").onclick = openWebLivePanel;
$("web-live-settings").onclick = openSettings;
$("web-live-connect").onclick = checkWebLiveConnection;
$("web-live-refresh").onclick = () => refreshWebLive();
$("web-live-provider").onchange = () => { stopWebLive(true); refreshWebLive(); };
$("web-live-login").onclick = async () => {
  const button = $("web-live-login"), connection = $("web-live-connection");
  button.disabled = true;
  $("web-live-connect").disabled = true;
  button.textContent = "正在打开…";
  connection.textContent = "正在启动专用登录窗口并检查连接…";
  try {
    const result = await webLiveAction("/api/web-advisors/login");
    connection.textContent = result.message;
  } catch (error) {
    connection.textContent = `打开专用浏览器失败：${error.message}`;
  } finally {
    button.textContent = "打开专用浏览器登录";
    button.disabled = false;
    $("web-live-connect").disabled = false;
  }
};
$("web-live-focus").onclick = async () => {
  const owner = webLive.owner, job = webLive.job, epoch = webLive.epoch;
  if (!owner || !job) return;
  $("web-live-focus").disabled = true;
  try {
    const result = await webLiveAction(`/api/sessions/${encodeURIComponent(owner.sid)}/web-advisors/${encodeURIComponent(job)}/focus?${new URLSearchParams({mode:owner.mode})}`);
    if (epoch === webLive.epoch) $("web-live-image-status").textContent = result.message;
  } catch (error) { if (epoch === webLive.epoch) $("web-live-image-status").textContent = error.message; }
  finally { if (epoch === webLive.epoch) $("web-live-focus").disabled = false; }
};
$("web-live-preview").onclick = () => {
  if (webLive.preview) {
    stopWebLive();
    pollWebLive(webLive.epoch);
    return;
  }
  if (!confirm("显示这次任务的官网只读镜像？画面可能包含本次对话，约每 2 秒刷新；不保存、不发给模型。密码/验证码页面会暂停。交互请在官网窗口完成。")) return;
  webLive.preview = true;
  webLive.lastImage = 0;
  $("web-live-preview").textContent = "停止官网画面";
  $("web-live-preview").setAttribute("aria-pressed", "true");
  $("web-live-image-status").textContent = "等待下次画面刷新…";
};
$("web-live-enlarge").onclick = () => { if ($("web-live-image").getAttribute("src")) $("web-live-dialog").showModal(); };
$("web-live-dialog-close").onclick = () => $("web-live-dialog").close();
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopWebLive();
  else if (webLiveVisible() && webLive.job) pollWebLive(webLive.epoch);
});
$("up").onclick = () => loadFiles(state.folder.split("/").slice(0, -1).join("/"));
$("refresh").onclick = () => loadFiles();
$("attach").onclick = () => {
  if (state.file?.attach && !state.attachments.includes(state.file.path)) {
    state.attachments.push(state.file.path);
    if (!desktopViewer.matches || app.classList.contains("viewer-expanded")) {
      showViewer(false);
      if (matchMedia("(max-width: 830px)").matches) showPanel(false);
    }
    drawAttachments();
    $("prompt").focus();
  }
};
$("prompt").oninput = controls;
$("stop").onclick = () => {
  if (state.stopping || liveSocket?.readyState !== WebSocket.OPEN) return;
  state.stopping = true;
  liveSocket.send(JSON.stringify({kind:"stop"}));
  status("正在请求停止…已开始的工具需安全退出");
  controls();
};
$("top-stop").onclick = () => $("stop").click();
$("interrupt-send").onclick = () => {
  const text = $("prompt").value.trim();
  if (!text || state.stopping || liveSocket?.readyState !== WebSocket.OPEN) return;
  stopAndContinue = {text};
  $("prompt").value = "";
  $("stop").click();
};
$("composer").onsubmit = send;
$("prompt").onkeydown = (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
};
bindSplitter("rail", $("sidebar"), 1);
bindSplitter("panel", $("panel"), -1);
bindSplitter("viewer", $("viewer"), -1);
showPanel(!matchMedia("(max-width: 830px)").matches);
const initialMode = localStorage.getItem("astro-mode");
if (["agent","research","code"].includes(initialMode)) { state.taskMode = initialMode; $("mode").value = initialMode; }
changeMode(["agent","research","code","chat"].includes(initialMode) ? initialMode : "agent");
checkEngine();
