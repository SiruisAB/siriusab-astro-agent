"use strict";

const $ = (id) => document.getElementById(id);
const app = document.querySelector(".app");
const desktopViewer = matchMedia("(min-width: 1100px)");
const tokenFormat = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1});
const state = {
  mode: "agent", taskMode: "agent", session: null, sessions: [], busy: false, loading: false,
  folder: "", file: null, attachments: [], view: 0, fileView: 0,
};
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
  $("send").disabled = locked || !$("model").value || !$("prompt").value.trim();
  for (const id of ["mode", "model", "new", "nav-task", "nav-chat"]) $(id).disabled = locked;
  for (const button of $("sessions").querySelectorAll("button")) button.disabled = locked;
  $("attach").hidden = state.mode !== "chat";
  $("attach").disabled = locked || state.mode !== "chat" || !state.file?.attach || state.attachments.length >= 8;
}

function drawSessions() {
  $("sessions").replaceChildren();
  for (const session of state.sessions) {
    const button = text("button", "", "session");
    button.title = session.title;
    button.setAttribute("aria-current", String(session.id === state.session));
    button.append(text("span", state.mode === "chat" ? "◌" : "◇", "session-icon"), text("span", session.title, "label"));
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
    $("model").append(new Option(`${profile.id} · ${profile.model}`, profile.id));
  }
  if (data.profiles.some((profile) => profile.id === previous)) $("model").value = previous;
  if (!data.profiles.length) $("model").append(new Option("未配置模型", ""));
  $("hint").textContent = data.profiles.length ? "Enter 发送 · Shift + Enter 换行" : "在 .env 中配置模型后重启工作台";
  drawSessions();
}

function showPanel(visible) {
  $("panel").hidden = !visible;
  app.classList.toggle("panel-closed", !visible);
  $("panel-toggle").setAttribute("aria-expanded", String(visible));
}

function expandViewer(expanded) {
  app.classList.toggle("viewer-expanded", expanded);
  const button = $("viewer-expand");
  const label = expanded ? "????" : "????";
  button.textContent = expanded ? "?" : "?";
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
  state.session = sid;
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
    scrollBottom();
  } catch (error) {
    resetView();
    status(error.message);
  } finally {
    state.loading = false;
    controls();
  }
}

function showTab(tab) {
  for (const name of ["activity", "files"]) {
    $(name).hidden = name !== tab;
    $(`${name}-tab`).setAttribute("aria-pressed", String(name === tab));
  }
  if (tab === "files") loadFiles();
}

function activity(message, kind = "") {
  $("activity-empty").hidden = true;
  const event = text("div", message, `event ${kind}`);
  $("events").append(event);
  return event;
}

function approve(event, socket) {
  expandViewer(false);
  if (!desktopViewer.matches) showViewer(false);
  showPanel(true);
  showTab("activity");
  status("等待命令审批");
  const card = activity("以下命令需要你的审批", "approval");
  for (const action of event.actions) card.append(text("pre", `${action.name}\n${action.command}`));
  const actions = text("div", "", "approval-actions");
  const allow = text("button", "批准执行", "small-button");
  const reject = text("button", "拒绝", "small-button");
  for (const [button, choice] of [[allow, "approve"], [reject, "reject"]]) {
    button.onclick = () => {
      if (socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({kind: "decision", id: event.id, choice}));
      actions.replaceChildren(text("span", choice === "approve" ? "已批准" : "已拒绝"));
      status("继续处理…");
    };
    actions.append(button);
  }
  card.append(actions);
  card.scrollIntoView({block: "nearest"});
  reject.focus({preventScroll: true});
}

function send(event) {
  event.preventDefault();
  const question = $("prompt").value.trim();
  if (state.busy || state.loading || !question || !$("model").value) return;
  const payload = {mode: state.mode, profile: $("model").value, session: state.session, text: question, attachments: [...state.attachments]};
  const socket = new WebSocket(`ws://${location.host}/api/run`);
  state.busy = true;
  showUsage();
  $("prompt").value = "";
  state.attachments = [];
  drawAttachments();
  const label = payload.attachments.length ? `${question}\n\n附件：${payload.attachments.join("、")}` : question;
  appendMessage("user", label);
  scrollBottom();
  controls();
  status("连接中…");
  let response = null;
  let draft = null;
  let draftBody = null;
  let finished = false;
  let failed = false;
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
    activity("已提交");
  };
  socket.onmessage = ({data}) => {
    const item = JSON.parse(data);
    if (item.kind === "session") state.session = item.id;
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
    if (item.kind === "approval") approve(item, socket);
    if (item.kind === "tool") activity(`${item.status === "error" ? "工具出错" : "工具完成"} · ${item.name}`);
    if (item.kind === "done") {
      const follow = atBottom();
      flush();
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
    for (const button of $("events").querySelectorAll(".approval-actions button")) button.disabled = true;
    try {
      await loadState();
      status(finished ? "" : "本轮未完成");
    } catch (error) {
      status(error.message);
    } finally {
      state.busy = false;
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
      button.append(text("span", entry.folder ? "▸" : "·", "file-icon"), text("span", entry.name, "label"));
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
  localStorage.setItem("astro-profile", $("model").value);
  showUsage();
  controls();
};
$("new").onclick = () => {
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
changeMode("agent");
