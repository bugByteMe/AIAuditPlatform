import { api, apiUrl, authenticatedApiUrl, uploadApi } from "./js/api.js";
import { applyLocale, t } from "./js/i18n.js";
import { state } from "./js/state.js";
import {
  renderAdmin,
  renderArtifacts,
  renderChatSessions,
  renderCurrentUser,
  renderEvents,
  renderFileExplorer,
  renderOperationProgress,
  renderPanelState,
  renderWorkspaceManagement,
  renderWorkspaces,
} from "./js/render.js";
import { currentWorkspace } from "./js/selectors.js";

const VALID_VIEWS = new Set(["workspace", "chat", "admin"]);
const LIVE_CHAT_STATES = new Set(["queued", "starting", "running", "stopping"]);
let activeChatStream = null;
let activePollTimer = null;

export function renderDynamic() {
  renderCurrentUser();
  renderWorkspaces();
  renderWorkspaceManagement();
  renderChatSessions();
  renderEvents();
  renderArtifacts();
  renderFileExplorer();
  renderOperationProgress();
  renderPanelState();
  renderAdmin();
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => toast.classList.remove("show"), 2600);
}

function safeCurrentWorkspace() {
  return currentWorkspace();
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function stopChatStream() {
  if (activeChatStream) activeChatStream.close();
  activeChatStream = null;
  if (activePollTimer) window.clearTimeout(activePollTimer);
  activePollTimer = null;
}

function mergeSessionEvents(session, events) {
  if (!session || !events.length) return false;
  session.events = session.events || [];
  const existingCount = state.chatLastEventIds[session.id] || 0;
  events
    .filter((event) => Number(event.id) > existingCount)
    .forEach((event) => {
      const toolCallId = event.toolCallId || "";
      const existingToolIndex =
        toolCallId && ["command", "tool"].includes(event.type)
          ? session.events.findIndex((item) => item[0] === event.type && item[3] === (event.runId || "") && item[6] === toolCallId)
          : -1;
      const tuple = [event.type, event.message, event.message, event.runId || "", Number(event.id) || 0, event.status || "", toolCallId];
      if (existingToolIndex >= 0) session.events[existingToolIndex] = tuple;
      else session.events.push(tuple);
      state.chatLastEventIds[session.id] = Number(event.id);
      if (["queued", "starting", "running", "stopping", "stopped", "completed", "failed"].includes(event.type)) {
        session.status = event.type === "running" ? "running" : event.type;
      }
      session.updated = event.time || session.updated;
    });
  return true;
}

function replaceWorkspace(updatedWorkspace) {
  const index = workspaceIndexById(updatedWorkspace.id);
  if (index >= 0) state.workspaces[index] = updatedWorkspace;
}

function setOperationProgress(label, value = 0, indeterminate = false) {
  state.operationProgress = { label, value, indeterminate };
  renderOperationProgress();
}

function clearOperationProgress() {
  state.operationProgress = null;
  renderOperationProgress();
}

async function refreshCurrentWorkspace() {
  const workspace = safeCurrentWorkspace();
  if (!workspace) return;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}`);
    replaceWorkspace(result.workspace);
    renderDynamic();
  } catch (error) {
    showToast(error.message || t("toast.workspaceLoadFailed"));
  }
}

function currentSessionObject() {
  const workspace = safeCurrentWorkspace();
  return workspace?.sessions?.[state.selectedSession] || workspace?.sessions?.[0] || null;
}

async function pollChatEvents(workspaceId, sessionId) {
  const after = state.chatLastEventIds[sessionId] || 0;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/chat/events?${new URLSearchParams({ sessionId, after }).toString()}`);
    const session = currentSessionObject();
    const changed = mergeSessionEvents(session, result.events || []);
    if (changed) renderDynamic();
    if (!session || !LIVE_CHAT_STATES.has(session.status)) {
      await refreshCurrentWorkspace();
      stopChatStream();
      return;
    }
  } catch (error) {
    showToast(error.message);
  }
  activePollTimer = window.setTimeout(() => pollChatEvents(workspaceId, sessionId), 2000);
}

function startChatStreamForSession(workspaceId, sessionId) {
  if (!workspaceId || !sessionId) return;
  stopChatStream();
  const after = state.chatLastEventIds[sessionId] || 0;
  const url = authenticatedApiUrl(`/api/workspaces/${encodeURIComponent(workspaceId)}/chat/stream?${new URLSearchParams({ sessionId, after }).toString()}`);
  activeChatStream = new EventSource(url, { withCredentials: true });
  const handleEvent = (event) => {
    const payload = JSON.parse(event.data);
    const session = currentSessionObject();
    if (mergeSessionEvents(session, [payload])) renderDynamic();
    if (["completed", "stopped", "failed"].includes(payload.type)) {
      refreshCurrentWorkspace();
      stopChatStream();
    }
  };
  activeChatStream.onmessage = handleEvent;
  ["user", "queued", "starting", "running", "stopping", "assistant", "command", "websearch", "tool", "usage", "progress", "error", "completed", "stopped", "failed"].forEach((type) => {
    activeChatStream.addEventListener(type, handleEvent);
  });
  activeChatStream.onerror = () => {
    if (activeChatStream) activeChatStream.close();
    activeChatStream = null;
    if (!activePollTimer) pollChatEvents(workspaceId, sessionId);
  };
}

function maybeStartChatStream() {
  const workspace = safeCurrentWorkspace();
  const session = currentSessionObject();
  if (!workspace || !session || !LIVE_CHAT_STATES.has(session.status)) {
    stopChatStream();
    return;
  }
  startChatStreamForSession(workspace.id, session.id);
}

async function loadWorkspaces() {
  try {
    const result = await api("/api/workspaces");
    state.workspaces = result.workspaces || [];
    state.workspacesLoaded = true;
    state.selectedWorkspace = Math.min(state.selectedWorkspace, Math.max(state.workspaces.length - 1, 0));
    state.selectedSession = 0;
    const workspace = safeCurrentWorkspace();
    state.selectedArtifacts = new Set(
      (workspace?.files || [])
        .filter((file) => file.type === "file")
        .map((file) => file.path),
    );
  } catch (error) {
    console.error("Failed to load workspaces", error);
    state.workspaces = [];
    state.workspacesLoaded = true;
    showToast(error.message || t("toast.workspaceLoadFailed"));
  }
  renderDynamic();
  maybeStartChatStream();
}

function startNativeDownload(url) {
  const anchor = document.createElement("a");
  anchor.href = apiUrl(url);
  anchor.rel = "noopener";
  anchor.style.display = "none";
  document.body.append(anchor);
  anchor.click();
  window.setTimeout(() => anchor.remove(), 1_000);
}

function viewFromLocation() {
  const view = window.location.hash.replace(/^#/, "");
  return VALID_VIEWS.has(view) ? view : "workspace";
}

function switchView(view) {
  const nextView = VALID_VIEWS.has(view) ? view : "workspace";
  document.body.classList.toggle("chat-view-active", nextView === "chat");
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === nextView);
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("active", section.id === `${nextView}-view`);
  });
}

function routeToView(view, { replace = false } = {}) {
  const nextView = VALID_VIEWS.has(view) ? view : "workspace";
  switchView(nextView);
  const nextHash = `#${nextView}`;
  if (window.location.hash === nextHash) return;
  const method = replace ? "replaceState" : "pushState";
  window.history[method](null, "", nextHash);
}

function showAuthenticated(user) {
  state.user = user;
  document.querySelector("#auth-screen").classList.add("hidden");
  document.querySelector("#app-shell").classList.remove("hidden");
  renderDynamic();
}

function showLogin() {
  state.user = null;
  document.querySelector("#auth-screen").classList.remove("hidden");
  document.querySelector("#app-shell").classList.add("hidden");
}

async function loadAccountControlData() {
  try {
    const [accounts, logs] = await Promise.all([api("/api/accounts"), api("/api/audit-logs")]);
    state.accounts = accounts.accounts || [];
    state.auditLogs = logs.logs || [];
  } catch {
    state.accounts = [];
    state.auditLogs = [];
  }
  renderAdmin();
}

function selectWorkspace(index) {
  state.selectedWorkspace = Number(index);
  state.selectedSession = 0;
  state.collapsedFileFolders = new Set();
  state.collapsedArtifactFolders = new Set();
  const workspace = safeCurrentWorkspace();
  state.selectedArtifacts = new Set(
    (workspace?.files || [])
      .filter((file) => file.type === "file")
      .map((file) => file.path),
  );
  renderDynamic();
  maybeStartChatStream();
}

function workspaceIndexById(id) {
  if (!id) return -1;
  return state.workspaces.findIndex((workspace) => workspace.id === id);
}

function resolveWorkspaceIndex(target) {
  const workspaceId = target?.dataset?.workspaceId;
  if (workspaceId) return workspaceIndexById(workspaceId);
  if (target?.hasAttribute("data-index")) return Number(target.dataset.index);
  if (target?.hasAttribute("data-workspace-card")) return Number(target.dataset.workspaceCard);
  return state.selectedWorkspace;
}

function selectWorkspaceFromElement(target) {
  const index = resolveWorkspaceIndex(target);
  if (index < 0 || !workspaceAt(index)) {
    showToast(t("toast.workspaceMissing"));
    return;
  }
  selectWorkspace(index);
}

function openWorkspaceModal() {
  state.selectedUploadFiles = [];
  document.querySelector("#workspace-create-form").reset();
  document.querySelector("#workspace-modal").classList.remove("hidden");
  document.querySelector("#workspace-name-input").focus();
  renderSelectedUploadFiles();
}

function closeWorkspaceModal() {
  if (state.workspaceCreateUploadController) {
    state.workspaceCreateUploadController.abort();
    state.workspaceCreateUploadController = null;
  }
  document.querySelector("#workspace-modal").classList.add("hidden");
}

function closeCodexSettingsModal() {
  document.querySelector("#codex-settings-modal").classList.add("hidden");
}

async function openCodexSettingsModal() {
  const modal = document.querySelector("#codex-settings-modal");
  const form = document.querySelector("#codex-settings-form");
  const status = document.querySelector("#codex-settings-status");
  form.reset();
  modal.classList.remove("hidden");
  try {
    const result = await api("/api/codex-settings");
    const settings = result.settings || {};
    document.querySelector("#codex-base-url-input").value = settings.baseUrl || "https://api.openai.com/v1";
    status.textContent = settings.apiKeyConfigured ? t("codex.configured") : t("codex.missing");
  } catch (error) {
    status.textContent = error.message;
  }
}

function closePreviewModal() {
  document.querySelector("#preview-modal").classList.add("hidden");
  document.querySelector("#preview-content").innerHTML = "";
  document.querySelector("#preview-download").dataset.downloadUrl = "";
}

function closeFileContextMenu() {
  const menu = document.querySelector("#file-context-menu");
  if (!menu) return;
  menu.classList.add("hidden");
  menu.dataset.filePath = "";
  menu.dataset.fileType = "";
}

function uniqueTopFolder(rootName) {
  const used = new Set(state.selectedUploadFiles.map((item) => item.path.split("/")[0]));
  if (!used.has(rootName)) return rootName;
  let index = 2;
  while (used.has(`${rootName} (${index})`)) index += 1;
  return `${rootName} (${index})`;
}

function addUploadFiles(files) {
  const incoming = [...files];
  if (!incoming.length) return;
  const firstPath = incoming[0].webkitRelativePath || incoming[0].name;
  const rootName = firstPath.includes("/") ? firstPath.split("/")[0] : "files";
  const stagedRoot = uniqueTopFolder(rootName);
  incoming.forEach((file) => {
    const browserPath = file.webkitRelativePath || file.name;
    const parts = browserPath.split("/");
    const path = parts.length > 1 ? [stagedRoot, ...parts.slice(1)].join("/") : file.name;
    state.selectedUploadFiles.push({ file, path });
  });
  document.querySelector("#workspace-upload-input").value = "";
  renderSelectedUploadFiles();
}

function renderSelectedUploadFiles() {
  const roots = new Map();
  state.selectedUploadFiles.forEach((item) => {
    const root = item.path.split("/")[0];
    roots.set(root, (roots.get(root) || 0) + 1);
  });
  const summary = document.querySelector("#workspace-upload-summary");
  summary.textContent = roots.size
    ? `${roots.size} ${state.lang === "zh" ? "个文件夹" : "folders"} · ${state.selectedUploadFiles.length} ${state.lang === "zh" ? "个文件" : "files"}`
    : t("workspace.noFolders");
  document.querySelector("#selected-folder-list").innerHTML = [...roots.entries()]
    .map(
      ([folder, count]) => `
        <div class="selected-folder-row">
          <strong>${folder}</strong>
          <span>${count} ${state.lang === "zh" ? "个文件" : "files"}</span>
        </div>
      `,
    )
    .join("");
}

function copySession(index) {
  const workspace = safeCurrentWorkspace();
  if (!workspace) return;
  const sessions = workspace.sessions;
  const source = sessions[index];
  sessions.splice(index + 1, 0, {
    ...source,
    title: `${source.title} ${state.lang === "zh" ? "副本" : "Copy"}`,
    status: "stopped",
    events: source.events.map((item) => [...item]),
  });
  state.selectedSession = index + 1;
  showToast(t("toast.copySession"));
}

function deleteSession(index) {
  const workspace = safeCurrentWorkspace();
  if (!workspace) return;
  const sessions = workspace.sessions;
  if (sessions.length <= 1) return;
  sessions.splice(index, 1);
  state.selectedSession = Math.max(0, Math.min(state.selectedSession, sessions.length - 1));
  showToast(t("toast.deleteSession"));
}

async function createNewChatSession() {
  const workspace = safeCurrentWorkspace();
  if (!workspace) return;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/chat/sessions`, {
      method: "POST",
      body: JSON.stringify({ title: t("chat.newTitle") }),
    });
    const workspaceIndex = workspaceIndexById(workspace.id);
    if (workspaceIndex >= 0) {
      const sessions = [result.session, ...(workspace.sessions || [])];
      state.workspaces[workspaceIndex] = { ...workspace, sessions };
      state.selectedSession = 0;
    }
    routeToView("chat");
    renderDynamic();
    document.querySelector("#composer textarea")?.focus();
  } catch (error) {
    showToast(error.message);
  }
}

function bindGlobalClicks() {
  document.addEventListener("click", (event) => {
    const nav = event.target.closest(".nav-item");
    if (nav) {
      routeToView(nav.dataset.view);
      if (nav.dataset.view === "admin") loadAccountControlData();
      return;
    }

    const langButton = event.target.closest("[data-lang]");
    if (langButton) {
      state.lang = langButton.dataset.lang;
      applyLocale();
      renderDynamic();
      return;
    }

    if (event.target.closest("#collapse-sidebar")) {
      document.querySelector(".app-shell").classList.toggle("sidebar-collapsed");
      return;
    }

    if (event.target.closest("[data-open-workspace-modal]")) {
      openWorkspaceModal();
      return;
    }

    if (event.target.closest("[data-close-workspace-modal]")) {
      closeWorkspaceModal();
      return;
    }

    if (event.target.closest("#codex-settings-button")) {
      openCodexSettingsModal();
      return;
    }

    if (event.target.closest("#new-chat-button")) {
      createNewChatSession();
      return;
    }

    if (event.target.closest("[data-close-codex-settings]")) {
      closeCodexSettingsModal();
      return;
    }

    if (event.target.closest("[data-close-preview-modal]")) {
      closePreviewModal();
      return;
    }

    if (!event.target.closest("#file-context-menu")) {
      closeFileContextMenu();
    }

    const previewDownload = event.target.closest("#preview-download");
    if (previewDownload) {
      const url = previewDownload.dataset.downloadUrl;
      if (url) startNativeDownload(url);
      return;
    }

    if (event.target.matches("[data-artifact]")) {
      return;
    }

    const folderRow = event.target.closest("[data-folder-path]");
    if (folderRow) {
      const path = folderRow.dataset.folderPath;
      const collapsedSet = folderRow.dataset.tree === "artifacts" ? state.collapsedArtifactFolders : state.collapsedFileFolders;
      if (collapsedSet.has(path)) collapsedSet.delete(path);
      else collapsedSet.add(path);
      if (folderRow.dataset.tree === "artifacts") renderArtifacts();
      else renderFileExplorer();
      return;
    }

    const panelToggle = event.target.closest("[data-toggle-panel]");
    if (panelToggle) {
      if (panelToggle.dataset.togglePanel === "sessions") state.isSessionPanelCollapsed = !state.isSessionPanelCollapsed;
      if (panelToggle.dataset.togglePanel === "files") state.isFileExplorerCollapsed = !state.isFileExplorerCollapsed;
      renderPanelState();
      return;
    }

    const sessionAction = event.target.closest("[data-session-action]");
    if (sessionAction) {
      const index = Number(sessionAction.dataset.sessionIndex);
      if (sessionAction.dataset.sessionAction === "copy") copySession(index);
      if (sessionAction.dataset.sessionAction === "delete") deleteSession(index);
      renderDynamic();
      return;
    }

    const sessionButton = event.target.closest("[data-session]");
    if (sessionButton) {
      state.selectedSession = Number(sessionButton.dataset.session);
      renderDynamic();
      maybeStartChatStream();
      return;
    }

    const actionButton = event.target.closest("button[data-action]");
    if (actionButton) {
      event.preventDefault();
      event.stopPropagation();
      const index = resolveWorkspaceIndex(actionButton);
      if (index < 0 || !workspaceAt(index)) {
        showToast(t("toast.workspaceMissing"));
        return;
      }
      if (actionButton.dataset.action === "share") {
        toggleWorkspaceSharing(index);
        return;
      }
      if (actionButton.dataset.action === "fork") {
        forkWorkspace(index);
        return;
      }
      if (actionButton.dataset.action === "delete") {
        deleteWorkspace(index);
        return;
      }
      if (actionButton.dataset.action === "open") {
        openWorkspace(index);
        return;
      }
    }

    const workspaceCard = event.target.closest("[data-workspace-card]");
    if (workspaceCard) selectWorkspaceFromElement(workspaceCard);
  });

  document.addEventListener("contextmenu", (event) => {
    const row = event.target.closest('[data-context-menu="artifact-file"]');
    if (!row) return;
    event.preventDefault();
    const menu = document.querySelector("#file-context-menu");
    menu.dataset.filePath = row.dataset.filePath;
    menu.dataset.fileType = row.dataset.fileType;
    menu.style.left = `${Math.min(event.clientX, window.innerWidth - 180)}px`;
    menu.style.top = `${Math.min(event.clientY, window.innerHeight - 90)}px`;
    menu.classList.remove("hidden");
  });
}

function workspaceAt(index) {
  return state.workspaces[index] || null;
}

function openWorkspace(index = state.selectedWorkspace) {
  if (!workspaceAt(index)) return;
  selectWorkspace(index);
  showToast(t("toast.open"));
  routeToView("chat");
}

async function forkWorkspace(index = state.selectedWorkspace) {
  const workspace = workspaceAt(index);
  if (!workspace) return;
  setOperationProgress(t("progress.fork"), 100, true);
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/fork`, {
      method: "POST",
      body: JSON.stringify({ name: `${workspace.name} Copy` }),
    });
    await loadWorkspaces();
    const forkIndex = state.workspaces.findIndex((item) => item.id === result.workspace.id);
    if (forkIndex >= 0) selectWorkspace(forkIndex);
    showToast(t("toast.fork"));
  } catch (error) {
    showToast(error.message);
  } finally {
    clearOperationProgress();
  }
}

async function createWorkspaceFromModal(form) {
  if (state.workspaceCreateUploadController) return;
  if (!state.selectedUploadFiles.length) {
    showToast(t("toast.createNeedsFiles"));
    return;
  }
  const formData = new FormData(form);
  const workspaceName = String(formData.get("workspaceName") || "").trim();
  const upload = new FormData();
  upload.append("name", workspaceName);
  upload.append("shared", document.querySelector("#workspace-shared-input").checked ? "true" : "false");
  state.selectedUploadFiles.forEach((item) => {
    upload.append("paths", item.path);
    upload.append("files", item.file, item.path);
  });
  const controller = new AbortController();
  const submitButton = document.querySelector("#workspace-create-submit");
  state.workspaceCreateUploadController = controller;
  submitButton.disabled = true;
  setOperationProgress(t("progress.uploadWorkspace"), 0);
  try {
    const result = await uploadApi(
      "/api/workspaces",
      upload,
      (percent) => setOperationProgress(t("progress.uploadWorkspace"), percent),
      { signal: controller.signal },
    );
    state.workspaceCreateUploadController = null;
    closeWorkspaceModal();
    await loadWorkspaces();
    const index = state.workspaces.findIndex((item) => item.id === result.workspace.id);
    if (index >= 0) selectWorkspace(index);
    showToast(state.lang === "zh" ? "工作区已创建。" : "Workspace created.");
  } catch (error) {
    if (error.name !== "AbortError") showToast(error.message);
  } finally {
    if (state.workspaceCreateUploadController === controller) state.workspaceCreateUploadController = null;
    submitButton.disabled = false;
    clearOperationProgress();
  }
}

function downloadCurrentWorkspace(mode) {
  const workspace = safeCurrentWorkspace();
  if (!workspace) return;
  const params = new URLSearchParams({ mode: "full" });
  selectedWorkspaceFilePaths(workspace).forEach((path) => params.append("paths", path));
  startNativeDownload(authenticatedApiUrl(`/api/workspaces/${encodeURIComponent(workspace.id)}/download?${params.toString()}`));
  showToast(t("toast.download"));
}

function fileUrl(workspaceId, action, path, extra = {}) {
  const params = new URLSearchParams({ path, ...extra });
  return authenticatedApiUrl(`/api/workspaces/${encodeURIComponent(workspaceId)}/files/${action}?${params.toString()}`);
}

function workspaceFileDownloadUrl(workspace, path, type) {
  if (type === "file") {
    return fileUrl(workspace.id, "raw", path, { download: "1" });
  }
  const params = new URLSearchParams({ mode: "full" });
  const descendantFiles = (workspace.files || [])
    .filter((file) => file.type === "file" && file.path.startsWith(`${path}/`))
  if (!descendantFiles.length) return "";
  descendantFiles.forEach((file) => params.append("paths", file.path));
  return authenticatedApiUrl(`/api/workspaces/${encodeURIComponent(workspace.id)}/download?${params.toString()}`);
}

function selectedWorkspaceFilePaths(workspace = safeCurrentWorkspace()) {
  if (!workspace) return [];
  const allowed = new Set((workspace.files || []).filter((file) => file.type === "file").map((file) => file.path));
  return [...state.selectedArtifacts].filter((path) => allowed.has(path));
}

function descendantFilePaths(workspace, folderPath) {
  return (workspace?.files || [])
    .filter((file) => file.type === "file" && file.path.startsWith(`${folderPath}/`))
    .map((file) => file.path);
}

async function openFilePreview(path) {
  const workspace = safeCurrentWorkspace();
  if (!workspace || !path) return;
  const modal = document.querySelector("#preview-modal");
  const title = document.querySelector("#preview-title");
  const meta = document.querySelector("#preview-meta");
  const content = document.querySelector("#preview-content");
  const downloadButton = document.querySelector("#preview-download");
  modal.classList.remove("hidden");
  title.textContent = path.split("/").pop();
  meta.textContent = t("preview.loading");
  content.innerHTML = `<div class="preview-empty">${t("preview.loading")}</div>`;
  downloadButton.dataset.downloadUrl = fileUrl(workspace.id, "raw", path, { download: "1" });
  try {
    const result = await api(fileUrl(workspace.id, "preview", path));
    const preview = result.preview;
    meta.textContent = `${preview.path} · ${preview.size} · ${preview.contentType}`;
    if (preview.mode === "text") {
      const node = document.createElement("pre");
      node.className = "preview-text";
      node.textContent = `${preview.text}${preview.truncated ? `\n\n${t("preview.truncated")}` : ""}`;
      content.replaceChildren(node);
    } else if (preview.mode === "image") {
      const node = document.createElement("img");
      node.className = "preview-image";
      node.alt = preview.name;
      node.src = fileUrl(workspace.id, "raw", preview.path);
      content.replaceChildren(node);
    } else if (preview.mode === "pdf") {
      const node = document.createElement("iframe");
      node.className = "preview-frame";
      node.title = preview.name;
      node.src = fileUrl(workspace.id, "raw", preview.path);
      content.replaceChildren(node);
    } else if (preview.mode === "office") {
      const node = document.createElement("iframe");
      node.className = "preview-frame";
      node.title = preview.name;
      node.src = fileUrl(workspace.id, "rendered", preview.path);
      content.replaceChildren(node);
    } else {
      content.innerHTML = `<div class="preview-empty">${t("preview.unsupported")}</div>`;
    }
  } catch (error) {
    meta.textContent = path;
    content.innerHTML = `<div class="preview-empty">${error.message || t("preview.failed")}</div>`;
  }
}

async function toggleWorkspaceSharing(index = state.selectedWorkspace) {
  const workspace = workspaceAt(index);
  if (!workspace) return;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}`, {
      method: "PATCH",
      body: JSON.stringify({ shared: !workspace.shared }),
    });
    const workspaceIndex = state.workspaces.findIndex((item) => item.id === workspace.id);
    if (workspaceIndex >= 0) state.workspaces[workspaceIndex] = result.workspace;
    renderDynamic();
    showToast(result.workspace.shared ? t("workspace.group") : t("workspace.private"));
  } catch (error) {
    showToast(error.message);
  }
}

async function uploadFilesToCurrentWorkspace(files) {
  const workspace = safeCurrentWorkspace();
  const incoming = [...files];
  if (!workspace || !incoming.length) return;
  const upload = new FormData();
  incoming.forEach((file) => {
    const path = file.webkitRelativePath || file.name;
    upload.append("paths", path);
    upload.append("files", file, path);
  });
  setOperationProgress(t("progress.uploadFiles"), 0);
  try {
    const result = await uploadApi(
      `/api/workspaces/${encodeURIComponent(workspace.id)}/files`,
      upload,
      (percent) => setOperationProgress(t("progress.uploadFiles"), percent),
    );
    const index = workspaceIndexById(workspace.id);
    if (index >= 0) state.workspaces[index] = result.workspace;
    state.selectedArtifacts = new Set(
      (result.workspace.files || [])
        .filter((file) => file.type === "file")
        .map((file) => file.path),
    );
    document.querySelector("#workspace-add-menu").classList.add("hidden");
    renderDynamic();
    showToast(t("toast.filesUploaded"));
  } catch (error) {
    showToast(error.message);
  } finally {
    clearOperationProgress();
  }
}

async function deleteCurrentWorkspacePath(path) {
  const workspace = safeCurrentWorkspace();
  if (!workspace || !path) return;
  const ok = window.confirm(state.lang === "zh" ? `删除“${path}”？` : `Delete "${path}"?`);
  if (!ok) return;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/files?${new URLSearchParams({ path }).toString()}`, {
      method: "DELETE",
    });
    const index = workspaceIndexById(workspace.id);
    if (index >= 0) state.workspaces[index] = result.workspace;
    state.selectedArtifacts.delete(path);
    closeFileContextMenu();
    renderDynamic();
    showToast(t("toast.fileDeleted"));
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteSelectedWorkspacePaths() {
  const workspace = safeCurrentWorkspace();
  const paths = selectedWorkspaceFilePaths(workspace);
  if (!workspace || !paths.length) {
    showToast(t("toast.noFilesSelected"));
    return;
  }
  const ok = window.confirm(
    state.lang === "zh" ? `删除 ${paths.length} 个所选文件？` : `Delete ${paths.length} selected files?`,
  );
  if (!ok) return;
  try {
    let latestWorkspace = workspace;
    for (const path of paths) {
      const result = await api(`/api/workspaces/${encodeURIComponent(latestWorkspace.id)}/files?${new URLSearchParams({ path }).toString()}`, {
        method: "DELETE",
      });
      latestWorkspace = result.workspace;
    }
    const index = workspaceIndexById(workspace.id);
    if (index >= 0) state.workspaces[index] = latestWorkspace;
    paths.forEach((path) => state.selectedArtifacts.delete(path));
    renderDynamic();
    showToast(t("toast.fileDeleted"));
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteWorkspace(index = state.selectedWorkspace) {
  const workspace = workspaceAt(index);
  if (!workspace) return;
  const ok = window.confirm(
    state.lang === "zh"
      ? `删除工作区“${workspace.name}”？此操作会移除当前工作目录。`
      : `Delete workspace "${workspace.name}"? This removes the active workspace directory.`,
  );
  if (!ok) return;
  try {
    await api(`/api/workspaces/${encodeURIComponent(workspace.id)}`, { method: "DELETE" });
    state.selectedWorkspace = Math.max(0, Math.min(index, state.workspaces.length - 2));
    state.selectedSession = 0;
    state.collapsedFileFolders = new Set();
    state.collapsedArtifactFolders = new Set();
    await loadWorkspaces();
    showToast(t("toast.deleteWorkspace"));
  } catch (error) {
    showToast(error.message);
  }
}

function bindForms() {
  document.querySelector("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    document.querySelector("#login-error").textContent = "";
    try {
      const result = await api("/api/login", {
        method: "POST",
        body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
      });
      showAuthenticated(result.user);
      await loadWorkspaces();
      await loadAccountControlData();
    } catch (error) {
      document.querySelector("#login-error").textContent = `${t("auth.failed")} ${error.message || ""}`.trim();
    }
  });

  document.querySelector("#logout-button").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => null);
    showLogin();
    showToast(t("toast.logout"));
  });

  document.querySelector("#composer").addEventListener("submit", async (event) => {
    event.preventDefault();
    const workspace = safeCurrentWorkspace();
    if (!workspace) return;
    const textarea = event.currentTarget.querySelector("textarea");
    if (!textarea.value.trim()) return;
    const model = event.currentTarget.querySelector('[name="model"]').value;
    const reasoning = event.currentTarget.querySelector('[name="reasoning"]').value;
    const session = currentSessionObject();
    const prompt = textarea.value.trim();
    textarea.value = "";
    try {
      const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/chat/runs`, {
        method: "POST",
        body: JSON.stringify({ prompt, sessionId: session?.id, model, reasoning }),
      });
      const workspaceIndex = workspaceIndexById(workspace.id);
      if (workspaceIndex >= 0) {
        const sessions = [...(workspace.sessions || [])];
        const existingIndex = sessions.findIndex((item) => item.id === result.session.id);
        if (existingIndex >= 0) sessions[existingIndex] = result.session;
        else sessions.unshift(result.session);
        state.workspaces[workspaceIndex] = { ...workspace, locked: true, sessions };
        state.selectedSession = Math.max(0, sessions.findIndex((item) => item.id === result.session.id));
      }
      state.chatLastEventIds[result.session.id] = result.session.events?.length || 0;
      renderDynamic();
      startChatStreamForSession(workspace.id, result.session.id);
      showToast(t("toast.send"));
    } catch (error) {
      textarea.value = prompt;
      showToast(error.message);
    }
  });

  document.querySelector("#workspace-create-form").addEventListener("submit", (event) => {
    event.preventDefault();
    createWorkspaceFromModal(event.currentTarget);
  });

  document.querySelector("#codex-settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const apiKey = String(form.get("apiKey") || "").trim();
    const clearCodexApiKey = form.get("clearCodexApiKey") === "on";
    const payload = {
      baseUrl: form.get("baseUrl"),
      clearCodexApiKey,
    };
    if (apiKey || clearCodexApiKey) payload.apiKey = apiKey;
    try {
      const result = await api("/api/codex-settings", {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      state.user = {
        ...state.user,
        codex: {
          baseUrl: result.settings.baseUrl,
          apiKeyConfigured: result.settings.apiKeyConfigured,
        },
      };
      closeCodexSettingsModal();
      renderCurrentUser();
      showToast(t("toast.codexSettingsSaved"));
    } catch (error) {
      document.querySelector("#codex-settings-status").textContent = error.message;
    }
  });
}

function bindInputs() {
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePreviewModal();
      closeWorkspaceModal();
      closeCodexSettingsModal();
      closeFileContextMenu();
      return;
    }
    const workspaceCard = event.target.closest("[data-workspace-card]");
    if (!workspaceCard || !["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    selectWorkspaceFromElement(workspaceCard);
  });

  document.querySelector("#stop-run").addEventListener("click", async () => {
    const workspace = safeCurrentWorkspace();
    if (!workspace) return;
    const session = currentSessionObject();
    const runId = session?.latestRunId;
    if (!runId) return;
    try {
      await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/chat/runs/${encodeURIComponent(runId)}/stop`, { method: "POST" });
      session.status = "stopping";
      renderDynamic();
      startChatStreamForSession(workspace.id, session.id);
      showToast(t("toast.stop"));
    } catch (error) {
      showToast(error.message);
    }
  });

  document.querySelector("#artifact-tree").addEventListener("change", (event) => {
    if (!event.target.matches("[data-artifact]")) return;
    const workspace = safeCurrentWorkspace();
    const path = event.target.dataset.artifact;
    const paths = event.target.dataset.artifactFolder ? descendantFilePaths(workspace, path) : [path];
    paths.forEach((itemPath) => {
      if (event.target.checked) state.selectedArtifacts.add(itemPath);
      else state.selectedArtifacts.delete(itemPath);
    });
    renderArtifacts();
  });

  document.querySelector("#select-all-artifacts").addEventListener("change", (event) => {
    const workspace = safeCurrentWorkspace();
    (workspace?.files || [])
      .filter((file) => file.type === "file")
      .forEach((file) => {
        if (event.target.checked) state.selectedArtifacts.add(file.path);
        else state.selectedArtifacts.delete(file.path);
      });
    renderArtifacts();
  });

  document.querySelector("#download-selected").addEventListener("click", () => downloadCurrentWorkspace("changes"));
  document.querySelector("#delete-selected-files").addEventListener("click", () => deleteSelectedWorkspacePaths());
  document.querySelector("#workspace-upload-input").addEventListener("change", (event) => addUploadFiles(event.target.files));
  document.querySelector("#add-folder-button").addEventListener("click", () => document.querySelector("#workspace-upload-input").click());
  document.querySelector("#workspace-add-menu-button").addEventListener("click", () => {
    document.querySelector("#workspace-add-menu").classList.toggle("hidden");
  });
  document.querySelector("#workspace-add-files-button").addEventListener("click", () => document.querySelector("#workspace-add-files-input").click());
  document.querySelector("#workspace-add-folder-button").addEventListener("click", () => document.querySelector("#workspace-add-folder-input").click());
  document.querySelector("#workspace-add-files-input").addEventListener("change", (event) => {
    uploadFilesToCurrentWorkspace(event.target.files);
    event.target.value = "";
  });
  document.querySelector("#workspace-add-folder-input").addEventListener("change", (event) => {
    uploadFilesToCurrentWorkspace(event.target.files);
    event.target.value = "";
  });
  document.querySelector("#file-context-menu").addEventListener("click", (event) => {
    const action = event.target.closest("[data-file-menu-action]");
    if (!action) return;
    const menu = document.querySelector("#file-context-menu");
    const workspace = safeCurrentWorkspace();
    const path = menu.dataset.filePath;
    const type = menu.dataset.fileType;
    if (!workspace || !path) return;
    if (action.dataset.fileMenuAction === "download") {
      const url = workspaceFileDownloadUrl(workspace, path, type);
      if (!url) {
        showToast(t("toast.noFilesToDownload"));
        closeFileContextMenu();
        return;
      }
      startNativeDownload(url);
      closeFileContextMenu();
      return;
    }
    if (action.dataset.fileMenuAction === "delete") {
      deleteCurrentWorkspacePath(path);
    }
  });
  document.querySelector("#sync-folder").addEventListener("click", () => {
    showToast("showDirectoryPicker" in window ? t("toast.download") : t("toast.syncUnavailable"));
  });

  document.addEventListener("dblclick", (event) => {
    const row = event.target.closest("[data-file-path]");
    if (!row || row.dataset.fileType !== "file") return;
    openFilePreview(row.dataset.filePath);
  });

  document.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest("[data-resize-panel]");
    if (!handle) return;
    const panel = handle.dataset.resizePanel;
    const startX = event.clientX;
    const startWidth = panel === "sessions" ? state.sessionPanelWidth : state.filePanelWidth;
    handle.classList.add("active");
    const onMove = (moveEvent) => {
      const delta = moveEvent.clientX - startX;
      if (panel === "sessions") state.sessionPanelWidth = clamp(startWidth + delta, 200, 520);
      if (panel === "files") state.filePanelWidth = clamp(startWidth - delta, 220, 560);
      renderPanelState();
    };
    const onUp = () => {
      handle.classList.remove("active");
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
    };
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
  });
}

async function bootstrap() {
  applyLocale();
  routeToView(viewFromLocation(), { replace: true });
  selectWorkspace(0);
  try {
    const session = await api("/api/session");
    showAuthenticated(session.user);
    await loadWorkspaces();
    await loadAccountControlData();
  } catch {
    showLogin();
  }
}

function syncRouteFromLocation() {
  const view = viewFromLocation();
  switchView(view);
  if (view === "admin" && state.user) loadAccountControlData();
}

window.addEventListener("hashchange", syncRouteFromLocation);
window.addEventListener("popstate", syncRouteFromLocation);

bindGlobalClicks();
bindForms();
bindInputs();
bootstrap();
