import { api, apiUrl, authenticatedApiUrl, uploadChunkApi } from "./js/api.js";
import {
  LIVE_CHAT_STATES,
  TERMINAL_CHAT_STATES,
  findSessionById,
  initializeEventCursors,
  mergeSessionEvents,
  sessionEventCursor,
} from "./js/chatEvents.js";
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
  renderWorkers,
  renderWorkspaces,
} from "./js/render.js";
import { currentWorkspace } from "./js/selectors.js";

const VALID_VIEWS = new Set(["workspace", "chat", "recharge", "admin"]);
let activeChatStream = null;
let activePollTimer = null;
let activeStreamGeneration = 0;
let workerStatusLoading = false;

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
  renderWorkers();
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

function escapeMarkup(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function stopChatStream() {
  activeStreamGeneration += 1;
  if (activeChatStream) activeChatStream.close();
  activeChatStream = null;
  if (activePollTimer) window.clearTimeout(activePollTimer);
  activePollTimer = null;
}

function sortTreeFiles(items) {
  const childrenByParent = new Map();
  items.forEach((item) => {
    const separator = item.path.lastIndexOf("/");
    const parent = separator < 0 ? "" : item.path.slice(0, separator);
    if (!childrenByParent.has(parent)) childrenByParent.set(parent, []);
    childrenByParent.get(parent).push(item);
  });
  const compareSiblings = (left, right) => {
    if (left.type !== right.type) return left.type === "folder" ? -1 : 1;
    return left.name.localeCompare(right.name);
  };
  const ordered = [];
  const seen = new Set();
  const appendChildren = (parent) => {
    (childrenByParent.get(parent) || []).sort(compareSiblings).forEach((item) => {
      if (seen.has(item.path)) return;
      seen.add(item.path);
      ordered.push(item);
      if (item.type === "folder") appendChildren(item.path);
    });
  };
  appendChildren("");
  items.filter((item) => !seen.has(item.path)).sort(compareSiblings).forEach((item) => ordered.push(item));
  return ordered;
}

function replaceWorkspace(updatedWorkspace) {
  const index = workspaceIndexById(updatedWorkspace.id);
  if (index < 0) return;
  const current = state.workspaces[index];
  const sameSnapshot = current?.latestSnapshotId === updatedWorkspace.latestSnapshotId;
  if (sameSnapshot) {
    const files = new Map((current.files || []).map((file) => [file.path, file]));
    (updatedWorkspace.files || []).forEach((file) => {
      const existing = files.get(file.path);
      files.set(file.path, {
        ...existing,
        ...file,
        ...(file.type === "folder" ? { childrenLoaded: Boolean(existing?.childrenLoaded || file.childrenLoaded) } : {}),
      });
    });
    state.workspaces[index] = { ...updatedWorkspace, files: sortTreeFiles([...files.values()]) };
    return;
  }
  state.workspaces[index] = updatedWorkspace;
  if (index === state.selectedWorkspace) resetFileTreeState(updatedWorkspace);
}

function resetFileTreeState(workspace) {
  const collapsed = (workspace?.files || [])
    .filter((file) => file.type === "folder" && file.hasChildren && !file.childrenLoaded)
    .map((file) => file.path);
  state.collapsedFileFolders = new Set(collapsed);
  state.collapsedArtifactFolders = new Set(collapsed);
  state.loadingFileFolders = new Set();
  state.selectedArtifacts = new Set(
    (workspace?.files || [])
      .filter((file) => Number(file.level || 0) === 0 && (file.type === "file" || file.hasChildren))
      .map((file) => file.path),
  );
}

function mergeWorkspaceTreeFiles(workspace, incomingFiles) {
  const files = new Map((workspace.files || []).map((file) => [file.path, file]));
  incomingFiles.forEach((file) => files.set(file.path, { ...files.get(file.path), ...file }));
  workspace.files = sortTreeFiles([...files.values()]);
}

async function expandWorkspaceFolder(path, collapsedFolders, renderTree) {
  const workspace = safeCurrentWorkspace();
  if (!workspace || state.loadingFileFolders.has(path)) return;
  const folder = (workspace.files || []).find((file) => file.type === "folder" && file.path === path);
  if (!folder?.hasChildren) return;
  if (folder.childrenLoaded) {
    collapsedFolders.delete(path);
    renderTree();
    return;
  }
  state.loadingFileFolders.add(path);
  renderTree();
  try {
    const params = new URLSearchParams({ path, depth: "1" });
    const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/files?${params.toString()}`);
    const latestWorkspace = state.workspaces.find((item) => item.id === workspace.id);
    if (!latestWorkspace) return;
    mergeWorkspaceTreeFiles(latestWorkspace, result.files || []);
    (result.files || [])
      .filter((file) => file.type === "folder" && file.hasChildren && !file.childrenLoaded)
      .forEach((file) => {
        state.collapsedFileFolders.add(file.path);
        state.collapsedArtifactFolders.add(file.path);
      });
    const latestFolder = latestWorkspace.files.find((file) => file.type === "folder" && file.path === path);
    if (latestFolder) latestFolder.childrenLoaded = true;
    collapsedFolders.delete(path);
  } catch (error) {
    showToast(error.message || t("toast.workspaceLoadFailed"));
  } finally {
    state.loadingFileFolders.delete(path);
    renderTree();
  }
}

async function refreshCurrentUser() {
  try {
    const result = await api("/api/session");
    if (result.user) state.user = result.user;
    renderCurrentUser();
  } catch (error) {
    console.warn("Current budget refresh failed", error);
  }
}

function nameEditorElement(location) {
  return [...document.querySelectorAll("[data-name-editor]")].find((input) => input.dataset.editorLocation === location);
}

function beginNameEdit(control) {
  const kind = control.dataset.nameKind;
  const id = control.dataset.nameId;
  const location = control.dataset.editorLocation;
  const workspace = kind === "workspace" ? state.workspaces.find((item) => item.id === id) : safeCurrentWorkspace();
  const session = kind === "session" ? workspace?.sessions?.find((item) => item.id === id) : null;
  const value = kind === "workspace" ? workspace?.name : session?.title;
  if (!value) return;
  state.nameEditor = { kind, id, location, draft: value, saving: false };
  renderDynamic();
  const input = nameEditorElement(location);
  input?.focus();
  input?.select();
}

function cancelNameEdit() {
  if (!state.nameEditor) return;
  state.nameEditor = null;
  renderDynamic();
}

async function commitNameEdit() {
  const editor = state.nameEditor;
  if (!editor || editor.saving) return;
  const title = editor.draft.trim();
  if (!title) {
    cancelNameEdit();
    return;
  }
  editor.saving = true;
  const workspace = editor.kind === "workspace" ? state.workspaces.find((item) => item.id === editor.id) : safeCurrentWorkspace();
  try {
    if (editor.kind === "workspace") {
      const result = await api(`/api/workspaces/${encodeURIComponent(editor.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ name: title }),
      });
      replaceWorkspace(result.workspace);
    } else if (workspace) {
      const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/chat/sessions/${encodeURIComponent(editor.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      const index = workspace.sessions.findIndex((item) => item.id === editor.id);
      if (index >= 0) workspace.sessions[index] = result.session;
    }
    if (state.nameEditor === editor) state.nameEditor = null;
    renderDynamic();
    showToast(t("toast.nameSaved"));
  } catch (error) {
    editor.saving = false;
    renderDynamic();
    if (state.nameEditor === editor) nameEditorElement(editor.location)?.focus();
    showToast(error.message);
  }
}

function setOperationProgress(label, value = 0, indeterminate = false) {
  state.operationProgress = { label, value, indeterminate };
  renderOperationProgress();
}

function clearOperationProgress() {
  state.operationProgress = null;
  renderOperationProgress();
}

async function refreshWorkspaceById(workspaceId) {
  if (!workspaceId) return;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}`);
    initializeEventCursors([result.workspace], state.chatLastEventIds);
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

function chatPollInterval() {
  return Math.max(250, Number(state.runtimeConfig.chatPollIntervalMs) || 2_000);
}

function scheduleChatPoll(workspaceId, sessionId, generation, delay = chatPollInterval()) {
  if (generation !== activeStreamGeneration || activePollTimer) return;
  activePollTimer = window.setTimeout(() => {
    activePollTimer = null;
    pollChatEvents(workspaceId, sessionId, generation);
  }, delay);
}

async function pollChatEvents(workspaceId, sessionId, generation) {
  if (generation !== activeStreamGeneration) return;
  const after = state.chatLastEventIds[sessionId] || 0;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/chat/events?${new URLSearchParams({ sessionId, after }).toString()}`);
    if (generation !== activeStreamGeneration) return;
    const session = findSessionById(state.workspaces, workspaceId, sessionId);
    if (!session) {
      stopChatStream();
      return;
    }
    const changed = mergeSessionEvents(session, result.events || [], state.chatLastEventIds);
    const serverStatus = result.sessionStatus || session.status;
    const statusChanged = serverStatus !== session.status;
    session.status = serverStatus;
    if (result.session) {
      session.resources = result.session.resources;
      session.workerId = result.session.workerId;
      session.container = result.session.container;
    }
    if (changed || statusChanged || result.session) renderDynamic();
    if (TERMINAL_CHAT_STATES.has(serverStatus)) {
      stopChatStream();
      await Promise.all([refreshWorkspaceById(workspaceId), refreshCurrentUser()]);
      return;
    }
  } catch (error) {
    console.warn("Chat event reconciliation failed", error);
  }
  scheduleChatPoll(workspaceId, sessionId, generation);
}

function startChatStreamForSession(workspaceId, sessionId) {
  if (!workspaceId || !sessionId) return;
  stopChatStream();
  const generation = activeStreamGeneration;
  const after = state.chatLastEventIds[sessionId] || 0;
  const url = authenticatedApiUrl(`/api/workspaces/${encodeURIComponent(workspaceId)}/chat/stream?${new URLSearchParams({ sessionId, after }).toString()}`);
  activeChatStream = new EventSource(url, { withCredentials: true });
  const handleEvent = (event) => {
    if (generation !== activeStreamGeneration) return;
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      console.warn("Invalid chat event payload", error);
      return;
    }
    const session = findSessionById(state.workspaces, workspaceId, sessionId);
    if (mergeSessionEvents(session, [payload], state.chatLastEventIds)) renderDynamic();
    if (TERMINAL_CHAT_STATES.has(payload.type)) {
      stopChatStream();
      refreshWorkspaceById(workspaceId);
      refreshCurrentUser();
    }
  };
  activeChatStream.onmessage = handleEvent;
  ["user", "queued", "starting", "running", "stopping", "assistant", "command", "websearch", "tool", "usage", "progress", "error", "completed", "stopped", "failed"].forEach((type) => {
    activeChatStream.addEventListener(type, handleEvent);
  });
  activeChatStream.onerror = () => {
    if (generation !== activeStreamGeneration) return;
    if (activeChatStream) activeChatStream.close();
    activeChatStream = null;
    if (activePollTimer) window.clearTimeout(activePollTimer);
    activePollTimer = null;
    scheduleChatPoll(workspaceId, sessionId, generation, 0);
  };
  scheduleChatPoll(workspaceId, sessionId, generation);
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
    initializeEventCursors(state.workspaces, state.chatLastEventIds);
    state.workspacesLoaded = true;
    state.selectedWorkspace = Math.min(state.selectedWorkspace, Math.max(state.workspaces.length - 1, 0));
    state.selectedSession = 0;
    const workspace = safeCurrentWorkspace();
    resetFileTreeState(workspace);
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
  if (nextView === "recharge" && state.user && !state.recharge) loadRechargeData();
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
  const isAdmin = user?.role === "system_admin";
  document.querySelector("#admin-nav-button").classList.toggle("hidden", !isAdmin);
  if (!isAdmin && viewFromLocation() === "admin") routeToView("workspace", { replace: true });
  document.querySelector("#auth-screen").classList.add("hidden");
  document.querySelector("#app-shell").classList.remove("hidden");
  renderDynamic();
  if (viewFromLocation() === "recharge" && !state.recharge) loadRechargeData();
}

function showLogin() {
  state.user = null;
  state.recharge = null;
  document.querySelector("#recharge-base-url").value = "";
  const rechargeKey = document.querySelector("#recharge-api-key");
  rechargeKey.value = "";
  rechargeKey.type = "password";
  document.querySelector("#recharge-key-visibility").textContent = t("recharge.show");
  document.querySelector("#auth-screen").classList.remove("hidden");
  document.querySelector("#app-shell").classList.add("hidden");
  setAuthMode("login");
}

async function loadAccountControlData() {
  if (state.user?.role !== "system_admin") {
    state.accounts = [];
    state.groups = [];
    state.workers = [];
    renderAdmin();
    renderWorkers();
    return;
  }
  try {
    const [accounts, logs, workers] = await Promise.all([api("/api/accounts"), api("/api/audit-logs"), api("/api/workers")]);
    state.accounts = accounts.accounts || [];
    state.groups = accounts.groups || [];
    state.auditLogs = logs.logs || [];
    state.workers = workers.workers || [];
  } catch {
    state.accounts = [];
    state.groups = [];
    state.auditLogs = [];
    state.workers = [];
  }
  renderAdmin();
  renderWorkers();
}

async function refreshWorkerStatus() {
  if (workerStatusLoading || state.user?.role !== "system_admin" || viewFromLocation() !== "admin") return;
  workerStatusLoading = true;
  try {
    const result = await api("/api/workers");
    state.workers = result.workers || [];
    renderWorkers();
  } catch (error) {
    console.warn("Worker status refresh failed", error);
  } finally {
    workerStatusLoading = false;
  }
}

async function loadRuntimeConfig() {
  try {
    const config = await api("/api/runtime-config");
    state.runtimeConfig = { ...state.runtimeConfig, ...config };
    document.querySelector('#register-form [name="password"]').minLength = state.runtimeConfig.registrationMinPasswordLength;
    document.querySelector('#batch-account-form [name="count"]').max = state.runtimeConfig.batchInviteMaxCount;
    document.querySelector('#batch-account-form [name="maxSessions"]').max = state.runtimeConfig.accountMaxSessionsLimit;
  } catch (error) {
    console.warn("Runtime configuration unavailable; using frontend defaults", error);
  }
}

function setAuthMode(mode) {
  const registering = mode === "register";
  document.querySelector("#login-form").classList.toggle("hidden", registering);
  document.querySelector("#register-form").classList.toggle("hidden", !registering);
  document.querySelectorAll("[data-auth-mode]").forEach((button) => button.classList.toggle("active", button.dataset.authMode === mode));
  const input = document.querySelector(registering ? '#register-form [name="inviteToken"]' : '#login-form [name="username"]');
  input?.focus();
}

function setGroupMode(mode) {
  const creating = mode === "new";
  document.querySelector("#existing-group-field").classList.toggle("hidden", creating);
  document.querySelector("#new-group-field").classList.toggle("hidden", !creating);
  document.querySelector("#batch-group-select").disabled = creating;
  document.querySelector("#batch-new-group").disabled = !creating;
  document.querySelector("#batch-new-group").required = creating;
  document.querySelectorAll("[data-group-mode]").forEach((button) => button.classList.toggle("active", button.dataset.groupMode === mode));
}

function renderGroupOptions() {
  document.querySelector("#batch-group-select").innerHTML = state.groups
    .map((group) => `<option value="${escapeMarkup(group.id)}">${escapeMarkup(group.name)}</option>`)
    .join("");
}

function renderCreatedInvites() {
  const results = document.querySelector("#invite-results");
  results.classList.toggle("hidden", !state.createdInvites.length);
  document.querySelector("#invite-result-list").innerHTML = state.createdInvites
    .map(
      (account) => `<div class="invite-result-row"><strong>${escapeMarkup(account.id)}</strong><code>${escapeMarkup(account.inviteToken)}</code><button type="button" class="btn" data-copy-invite="${escapeMarkup(account.inviteToken)}">${t("admin.copy")}</button></div>`,
    )
    .join("");
}

function openBatchAccountModal() {
  if (state.user?.role !== "system_admin") return;
  const form = document.querySelector("#batch-account-form");
  form.reset();
  state.createdInvites = [];
  renderGroupOptions();
  setGroupMode(state.groups.length ? "existing" : "new");
  renderCreatedInvites();
  document.querySelector("#batch-account-error").textContent = "";
  document.querySelector("#batch-account-modal").classList.remove("hidden");
}

function closeBatchAccountModal() {
  document.querySelector("#batch-account-modal").classList.add("hidden");
}

async function copyText(value, successKey = "toast.inviteCopied") {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
  } else {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }
  showToast(t(successKey));
}

async function loadRechargeData() {
  const error = document.querySelector("#recharge-error");
  error.textContent = t("recharge.loading");
  try {
    const result = await api("/api/recharge");
    state.recharge = result;
    document.querySelector("#recharge-base-url").value = result.baseUrl || "";
    document.querySelector("#recharge-api-key").value = result.apiKey || "";
    document.querySelector("#recharge-username").textContent = result.username || state.user?.username || "-";
    error.textContent = "";
  } catch (loadError) {
    state.recharge = null;
    error.textContent = loadError.message;
  }
}

function closeRechargeQrModal() {
  document.querySelector("#recharge-qr-modal").classList.add("hidden");
  document.querySelector("#recharge-qr-image").removeAttribute("src");
}

function openRechargeQrModal(amount) {
  const product = state.recharge?.products?.find((item) => Number(item.amountCny) === Number(amount));
  if (!product) {
    showToast(t("recharge.notReady"));
    if (!state.recharge) loadRechargeData();
    return;
  }
  const image = document.querySelector("#recharge-qr-image");
  const missing = document.querySelector("#recharge-qr-missing");
  const username = state.recharge.username || state.user?.username || "-";
  document.querySelector("#recharge-qr-amount").textContent = `¥${Number(product.amountCny).toFixed(0)}`;
  document.querySelector("#recharge-modal-username").textContent = username;
  image.classList.remove("hidden");
  missing.classList.add("hidden");
  image.onload = () => {
    image.classList.remove("hidden");
    missing.classList.add("hidden");
  };
  image.onerror = () => {
    image.classList.add("hidden");
    missing.classList.remove("hidden");
  };
  image.src = product.qrCodeUrl;
  document.querySelector("#recharge-qr-modal").classList.remove("hidden");
}

async function revokeInvite(userId) {
  try {
    await api(`/api/accounts/${encodeURIComponent(userId)}/revoke-invite`, { method: "POST" });
    await loadAccountControlData();
    showToast(t("toast.inviteRevoked"));
  } catch (error) {
    showToast(error.message);
  }
}

async function createAdminGroup() {
  const name = window.prompt(t("admin.promptGroupName"));
  if (name === null || !name.trim()) return;
  try {
    await api("/api/groups", { method: "POST", body: JSON.stringify({ name: name.trim() }) });
    await loadAccountControlData();
    showToast(t("toast.groupCreated"));
  } catch (error) {
    showToast(error.message);
  }
}

async function setGroupDiskLimit(groupId) {
  const group = state.groups.find((item) => item.id === groupId);
  const initial = group?.diskLimitBytes === null || group?.diskLimitBytes === undefined ? "" : String(Math.round(Number(group.diskLimitBytes) / (1024 * 1024)));
  const raw = window.prompt(t("admin.promptDiskLimit"), initial);
  if (raw === null) return;
  const trimmed = raw.trim();
  const mebibytes = trimmed === "" ? null : Number(trimmed);
  if (mebibytes !== null && (!Number.isFinite(mebibytes) || mebibytes < 0)) return;
  try {
    await api(`/api/groups/${encodeURIComponent(groupId)}`, {
      method: "PATCH",
      body: JSON.stringify({ diskLimitBytes: mebibytes === null ? null : Math.round(mebibytes * 1024 * 1024) }),
    });
    await loadAccountControlData();
    showToast(t("toast.groupLimitSaved"));
  } catch (error) {
    showToast(error.message);
  }
}

async function setGroupLiveRunLimit(groupId) {
  const group = state.groups.find((item) => item.id === groupId);
  const raw = window.prompt(t("admin.promptLiveRunLimit"), String(group?.liveRunLimit || 1));
  if (raw === null) return;
  const liveRunLimit = Number(raw.trim());
  if (!Number.isInteger(liveRunLimit) || liveRunLimit < 1) return;
  try {
    await api(`/api/groups/${encodeURIComponent(groupId)}`, {
      method: "PATCH",
      body: JSON.stringify({ liveRunLimit }),
    });
    await loadAccountControlData();
    showToast(t("toast.groupLiveRunLimitSaved"));
  } catch (error) {
    showToast(error.message);
  }
}

async function resetAccountBudget(userId) {
  const account = state.accounts.find((item) => item.id === userId);
  const raw = window.prompt(t("admin.promptBudget"), "0.00");
  if (raw === null) return;
  const amountCny = Number(raw.trim());
  if (!Number.isFinite(amountCny) || amountCny < 0) return;
  try {
    await api(`/api/accounts/${encodeURIComponent(userId)}/reset-budget`, {
      method: "POST",
      body: JSON.stringify({ amountCny: amountCny.toFixed(2) }),
    });
    await loadAccountControlData();
    showToast(t("toast.budgetReset"));
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteAdminAccount(userId) {
  if (!window.confirm(t("admin.confirmDeleteUser"))) return;
  try {
    await api(`/api/accounts/${encodeURIComponent(userId)}`, { method: "DELETE" });
    await loadAccountControlData();
    showToast(t("toast.accountDeleted"));
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteAdminGroup(groupId) {
  if (!window.confirm(t("admin.confirmDeleteGroup"))) return;
  try {
    await api(`/api/groups/${encodeURIComponent(groupId)}`, { method: "DELETE" });
    await loadAccountControlData();
    showToast(t("toast.groupDeleted"));
  } catch (error) {
    showToast(error.message);
  }
}

function selectWorkspace(index) {
  state.selectedWorkspace = Number(index);
  state.selectedSession = 0;
  const workspace = safeCurrentWorkspace();
  resetFileTreeState(workspace);
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
    const mode = settings.mode || "micu";
    const modeInput = form.querySelector(`[name="mode"][value="${mode}"]`);
    if (modeInput) modeInput.checked = true;
    document.querySelector("#codex-base-url-input").value = settings.baseUrl || "https://api.openai.com/v1";
    const micuBudget = settings.micu?.budget || {};
    const remaining = micuBudget.remainingPercent === null || micuBudget.remainingPercent === undefined
      ? t("codex.balanceUnavailable")
      : `${Number(micuBudget.remainingPercent).toFixed(1)}%`;
    document.querySelector("#codex-micu-summary").textContent = `${t("codex.micuBalance")}: ${remaining} · ${settings.micu?.group || ""}`;
    document.querySelector("#codex-custom-fields").classList.toggle("hidden", mode !== "custom");
    status.textContent = mode === "micu" ? t("codex.usingMicu") : (settings.apiKeyConfigured ? t("codex.configured") : t("codex.missing"));
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

async function copySession(index) {
  const workspace = safeCurrentWorkspace();
  if (!workspace) return;
  const source = workspace.sessions[index];
  if (!source) return;
  try {
    const result = await api(
      `/api/workspaces/${encodeURIComponent(workspace.id)}/chat/sessions/${encodeURIComponent(source.id)}/fork`,
      {
        method: "POST",
        body: JSON.stringify({ title: `${source.title} ${state.lang === "zh" ? "副本" : "Copy"}` }),
      },
    );
    const workspaceIndex = workspaceIndexById(workspace.id);
    if (workspaceIndex < 0) return;
    const activeSourceRunId = LIVE_CHAT_STATES.has(source.status) ? source.latestRunId : null;
    const copiedEvents = (source.events || [])
      .filter((event) => !activeSourceRunId || event[3] !== activeSourceRunId)
      .map((event) => [...event]);
    result.session.events = result.session.events?.length ? result.session.events : copiedEvents;
    const sessions = [...(workspace.sessions || [])];
    sessions.splice(index + 1, 0, result.session);
    state.workspaces[workspaceIndex] = { ...workspace, sessions };
    state.selectedSession = index + 1;
    state.chatLastEventIds[result.session.id] = sessionEventCursor(result.session);
    renderDynamic();
    showToast(t("toast.copySession"));
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteSession(index) {
  const workspace = safeCurrentWorkspace();
  if (!workspace) return;
  const session = workspace.sessions[index];
  if (!session || !window.confirm(t("chat.confirmDelete"))) return;
  try {
    await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/chat/sessions/${encodeURIComponent(session.id)}`, {
      method: "DELETE",
    });
    const workspaceIndex = workspaceIndexById(workspace.id);
    if (workspaceIndex < 0) return;
    const latestWorkspace = state.workspaces[workspaceIndex];
    const selectedSessionId = latestWorkspace.sessions?.[state.selectedSession]?.id;
    const removedIndex = (latestWorkspace.sessions || []).findIndex((item) => item.id === session.id);
    const sessions = (latestWorkspace.sessions || []).filter((item) => item.id !== session.id);
    state.workspaces[workspaceIndex] = { ...latestWorkspace, sessions };
    delete state.chatLastEventIds[session.id];
    const retainedSelection = sessions.findIndex((item) => item.id === selectedSessionId);
    state.selectedSession = retainedSelection >= 0
      ? retainedSelection
      : Math.max(0, Math.min(Math.max(removedIndex, 0), sessions.length - 1));
    renderDynamic();
    maybeStartChatStream();
    showToast(t("toast.deleteSession"));
  } catch (error) {
    showToast(error.message);
  }
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
    const editableName = event.target.closest("[data-edit-name]");
    if (editableName) {
      event.preventDefault();
      event.stopPropagation();
      beginNameEdit(editableName);
      return;
    }

    const authMode = event.target.closest("[data-auth-mode]");
    if (authMode) {
      setAuthMode(authMode.dataset.authMode);
      return;
    }

    const groupMode = event.target.closest("[data-group-mode]");
    if (groupMode) {
      setGroupMode(groupMode.dataset.groupMode);
      return;
    }

    if (event.target.closest("#batch-create-button")) {
      openBatchAccountModal();
      return;
    }

    if (event.target.closest("#create-group-button")) {
      createAdminGroup();
      return;
    }

    const setGroupLimit = event.target.closest("[data-set-group-limit]");
    if (setGroupLimit) {
      event.preventDefault();
      setGroupDiskLimit(setGroupLimit.dataset.setGroupLimit);
      return;
    }

    const setGroupLiveRunLimitButton = event.target.closest("[data-set-group-live-run-limit]");
    if (setGroupLiveRunLimitButton) {
      event.preventDefault();
      setGroupLiveRunLimit(setGroupLiveRunLimitButton.dataset.setGroupLiveRunLimit);
      return;
    }

    const deleteGroup = event.target.closest("[data-delete-group]");
    if (deleteGroup) {
      event.preventDefault();
      deleteAdminGroup(deleteGroup.dataset.deleteGroup);
      return;
    }

    const resetBudget = event.target.closest("[data-reset-budget]");
    if (resetBudget) {
      resetAccountBudget(resetBudget.dataset.resetBudget);
      return;
    }

    const deleteAccount = event.target.closest("[data-delete-account]");
    if (deleteAccount) {
      deleteAdminAccount(deleteAccount.dataset.deleteAccount);
      return;
    }

    if (event.target.closest("[data-close-batch-account]")) {
      closeBatchAccountModal();
      return;
    }

    const copyInvite = event.target.closest("[data-copy-invite]");
    if (copyInvite) {
      copyText(copyInvite.dataset.copyInvite).catch((error) => showToast(error.message));
      return;
    }

    const revokeButton = event.target.closest("[data-revoke-invite]");
    if (revokeButton) {
      revokeInvite(revokeButton.dataset.revokeInvite);
      return;
    }

    if (event.target.closest("#copy-all-invites")) {
      const text = state.createdInvites.map((account) => `${account.id}\t${account.inviteToken}`).join("\n");
      if (text) copyText(text).catch((error) => showToast(error.message));
      return;
    }

    const nav = event.target.closest(".nav-item");
    if (nav) {
      if (nav.dataset.view === "admin" && state.user?.role !== "system_admin") return;
      routeToView(nav.dataset.view);
      if (nav.dataset.view === "admin") loadAccountControlData();
      return;
    }

    const rechargeProduct = event.target.closest("[data-recharge-amount]");
    if (rechargeProduct) {
      openRechargeQrModal(rechargeProduct.dataset.rechargeAmount);
      return;
    }

    const rechargeCopy = event.target.closest("[data-copy-recharge]");
    if (rechargeCopy) {
      const value = state.recharge?.[rechargeCopy.dataset.copyRecharge];
      if (value) copyText(value, "toast.credentialCopied").catch((error) => showToast(error.message));
      return;
    }

    if (event.target.closest("#recharge-key-visibility")) {
      const input = document.querySelector("#recharge-api-key");
      input.type = input.type === "password" ? "text" : "password";
      event.target.closest("#recharge-key-visibility").textContent = t(input.type === "password" ? "recharge.show" : "recharge.hide");
      return;
    }

    if (event.target.closest("[data-close-recharge-qr]")) {
      closeRechargeQrModal();
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
      const renderTree = folderRow.dataset.tree === "artifacts" ? renderArtifacts : renderFileExplorer;
      if (collapsedSet.has(path)) expandWorkspaceFolder(path, collapsedSet, renderTree);
      else {
        collapsedSet.add(path);
        renderTree();
      }
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
  const controller = new AbortController();
  const submitButton = document.querySelector("#workspace-create-submit");
  state.workspaceCreateUploadController = controller;
  submitButton.disabled = true;
  setOperationProgress(t("progress.uploadWorkspace"), 0);
  try {
    const workspace = await runResumableUpload({
      items: state.selectedUploadFiles,
      mode: "create",
      name: workspaceName,
      shared: document.querySelector("#workspace-shared-input").checked,
      signal: controller.signal,
      uploadLabel: t("progress.uploadWorkspace"),
    });
    state.workspaceCreateUploadController = null;
    closeWorkspaceModal();
    await loadWorkspaces();
    const index = state.workspaces.findIndex((item) => item.id === workspace.id);
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

const PENDING_UPLOAD_KEY = "aiAuditPendingUpload";

function uploadFingerprint(items, mode, workspaceId = "", name = "", shared = false) {
  return JSON.stringify({
    mode,
    workspaceId,
    name,
    shared,
    files: items.map((item) => [item.path, item.file.size, item.file.lastModified]),
  });
}

async function runResumableUpload({ items, mode, workspaceId = "", name = "", shared = false, signal, uploadLabel }) {
  const fingerprint = uploadFingerprint(items, mode, workspaceId, name, shared);
  let saved = null;
  try {
    saved = JSON.parse(window.localStorage.getItem(PENDING_UPLOAD_KEY) || "null");
  } catch {
    window.localStorage.removeItem(PENDING_UPLOAD_KEY);
  }
  let upload = null;
  if (saved?.fingerprint === fingerprint && saved.uploadId) {
    try {
      upload = (await api(`/api/uploads/${encodeURIComponent(saved.uploadId)}`)).upload;
    } catch {
      window.localStorage.removeItem(PENDING_UPLOAD_KEY);
    }
  }
  if (!upload || !["uploading", "processing", "committing"].includes(upload.status)) {
    upload = (await api("/api/uploads", {
      method: "POST",
      body: JSON.stringify({
        mode,
        workspaceId: workspaceId || undefined,
        name,
        shared,
        files: items.map((item) => ({ path: item.path, size: item.file.size, lastModified: item.file.lastModified })),
      }),
    })).upload;
    window.localStorage.setItem(PENDING_UPLOAD_KEY, JSON.stringify({ fingerprint, uploadId: upload.id }));
  }
  const total = Math.max(1, Number(upload.totalBytes || items.reduce((sum, item) => sum + item.file.size, 0)));
  const offsets = items.map((_, index) => Number(upload.offsets?.[index] || 0));
  const inflight = new Map();
  const renderBytes = () => {
    const sent = offsets.reduce((sum, value) => sum + value, 0) + [...inflight.values()].reduce((sum, value) => sum + value, 0);
    setOperationProgress(uploadLabel, Math.min(89, Math.floor((sent / total) * 89)));
  };
  if (upload.status === "uploading") {
    let nextIndex = 0;
    const sendFile = async () => {
      while (nextIndex < items.length) {
        const index = nextIndex++;
        const file = items[index].file;
        while (offsets[index] < file.size) {
          if (signal?.aborted) throw new DOMException("Upload aborted", "AbortError");
          const start = offsets[index];
          const end = Math.min(file.size, start + Number(upload.chunkSizeBytes || state.runtimeConfig.uploadChunkBytes || 8 * 1024 * 1024));
          let result;
          let failures = 0;
          while (!result) {
            try {
              result = await uploadChunkApi(
                `/api/uploads/${encodeURIComponent(upload.id)}/files/${index}?offset=${start}`,
                file.slice(start, end),
                (loaded) => { inflight.set(index, loaded); renderBytes(); },
                { signal },
              );
            } catch (error) {
              inflight.delete(index);
              renderBytes();
              if (signal?.aborted || (!error.retryable && Number(error.status || 0) < 500) || failures >= 2) throw error;
              failures += 1;
              await new Promise((resolve) => window.setTimeout(resolve, failures * 750));
            }
          }
          inflight.delete(index);
          offsets[index] = Number(result.upload.offsets?.[index] ?? end);
          renderBytes();
        }
      }
    };
    await Promise.all([sendFile(), sendFile()]);
    upload = (await api(`/api/uploads/${encodeURIComponent(upload.id)}/complete`, { method: "POST", body: JSON.stringify({}) })).upload;
  }
  while (upload.status !== "committed") {
    if (upload.status === "failed") throw new Error(upload.error || "Upload processing failed");
    if (signal?.aborted && upload.status === "uploading") throw new DOMException("Upload aborted", "AbortError");
    setOperationProgress(t(upload.phase === "committing" ? "progress.committingUpload" : "progress.processingUpload"), 90, true);
    await new Promise((resolve) => window.setTimeout(resolve, 500));
    upload = (await api(`/api/uploads/${encodeURIComponent(upload.id)}`)).upload;
  }
  window.localStorage.removeItem(PENDING_UPLOAD_KEY);
  setOperationProgress(t("progress.committingUpload"), 100);
  return upload.workspace;
}

function downloadCurrentWorkspace(mode) {
  const workspace = safeCurrentWorkspace();
  if (!workspace) return;
  const selectedPaths = selectedWorkspaceFilePaths(workspace);
  if (!selectedPaths.length) {
    showToast(t("toast.noFilesSelected"));
    return;
  }
  const params = new URLSearchParams({ mode: "full" });
  selectedPaths.forEach((path) => params.append("paths", path));
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
  params.append("paths", path);
  return authenticatedApiUrl(`/api/workspaces/${encodeURIComponent(workspace.id)}/download?${params.toString()}`);
}

function selectedWorkspaceFilePaths(workspace = safeCurrentWorkspace()) {
  if (!workspace) return [];
  const allowed = new Set((workspace.files || []).map((file) => file.path));
  return [...state.selectedArtifacts].filter((path) => allowed.has(path));
}

function directTreeChildren(workspace, folderPath) {
  const parentDepth = folderPath.split("/").length;
  return (workspace?.files || []).filter((file) => file.path.startsWith(`${folderPath}/`) && file.path.split("/").length === parentDepth + 1);
}

function deselectTreePath(workspace, path) {
  const selectedAncestor = [...state.selectedArtifacts]
    .filter((selected) => path === selected || path.startsWith(`${selected}/`))
    .sort((left, right) => right.length - left.length)[0];
  [...state.selectedArtifacts]
    .filter((selected) => selected === path || selected.startsWith(`${path}/`))
    .forEach((selected) => state.selectedArtifacts.delete(selected));
  if (!selectedAncestor) return;
  state.selectedArtifacts.delete(selectedAncestor);
  let branch = selectedAncestor;
  while (branch !== path) {
    const children = directTreeChildren(workspace, branch);
    const next = children.find((child) => path === child.path || path.startsWith(`${child.path}/`));
    if (!next) return;
    children
      .filter((child) => child.path !== next.path && (child.type === "file" || child.hasChildren))
      .forEach((child) => state.selectedArtifacts.add(child.path));
    branch = next.path;
  }
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
    replaceWorkspace(result.workspace);
    renderDynamic();
    showToast(result.workspace.shared ? t("workspace.group") : t("workspace.private"));
  } catch (error) {
    showToast(error.message);
  }
}

async function updateWorkspaceRunLock(enabled) {
  const workspace = safeCurrentWorkspace();
  if (!workspace) return;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}`, {
      method: "PATCH",
      body: JSON.stringify({ runLockEnabled: enabled }),
    });
    replaceWorkspace(result.workspace);
    renderDynamic();
    showToast(t("toast.workspaceLockUpdated"));
  } catch (error) {
    renderDynamic();
    showToast(error.message);
  }
}

async function uploadFilesToCurrentWorkspace(files) {
  const workspace = safeCurrentWorkspace();
  const incoming = [...files];
  if (!workspace || !incoming.length) return;
  const items = incoming.map((file) => ({ file, path: file.webkitRelativePath || file.name }));
  setOperationProgress(t("progress.uploadFiles"), 0);
  try {
    const updatedWorkspace = await runResumableUpload({ items, mode: "append", workspaceId: workspace.id, uploadLabel: t("progress.uploadFiles") });
    replaceWorkspace(updatedWorkspace);
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
    replaceWorkspace(result.workspace);
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
    replaceWorkspace(latestWorkspace);
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
    const loginError = document.querySelector("#login-error");
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    loginError.textContent = state.lang === "zh" ? "正在登录..." : "Signing in...";
    if (submitButton) submitButton.disabled = true;
    try {
      const result = await api("/api/login", {
        method: "POST",
        body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
      });
      loginError.textContent = state.lang === "zh" ? "登录成功，正在载入工作区..." : "Signed in. Loading workspaces...";
      showAuthenticated(result.user);
      await loadWorkspaces();
      await loadAccountControlData();
    } catch (error) {
      loginError.textContent = `${t("auth.failed")} ${error.message || ""}`.trim();
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  });

  document.querySelector("#register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const registerError = document.querySelector("#register-error");
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    registerError.textContent = t("auth.registering");
    if (submitButton) submitButton.disabled = true;
    try {
      const result = await api("/api/register", {
        method: "POST",
        body: JSON.stringify({ inviteToken: form.get("inviteToken"), username: form.get("username"), password: form.get("password") }),
      });
      showAuthenticated(result.user);
      await loadWorkspaces();
      await loadAccountControlData();
      event.currentTarget.reset();
    } catch (error) {
      registerError.textContent = `${t("auth.registerFailed")} ${error.message || ""}`.trim();
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  });

  document.querySelector("#batch-account-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const creatingGroup = !document.querySelector("#batch-new-group").disabled;
    const errorElement = document.querySelector("#batch-account-error");
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    const payload = {
      groupId: creatingGroup ? "" : form.get("groupId"),
      newGroupName: creatingGroup ? form.get("newGroupName") : "",
      count: Number(form.get("count")),
      budgetCny: String(form.get("budgetCny") || "0.00"),
      maxSessions: Number(form.get("maxSessions")),
    };
    errorElement.textContent = "";
    if (submitButton) submitButton.disabled = true;
    try {
      const result = await api("/api/accounts/batch", { method: "POST", body: JSON.stringify(payload) });
      state.createdInvites = result.accounts || [];
      await loadAccountControlData();
      renderGroupOptions();
      renderCreatedInvites();
      showToast(t("toast.invitesCreated"));
    } catch (error) {
      errorElement.textContent = error.message;
    } finally {
      if (submitButton) submitButton.disabled = false;
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
      const request = { prompt, sessionId: session?.id, model, reasoning };
      let result;
      while (!result) {
        try {
          result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/chat/runs`, {
            method: "POST",
            body: JSON.stringify(request),
          });
        } catch (error) {
          if (error.code !== "concurrent_confirmation_required" || request.confirmConcurrent) throw error;
          if (!window.confirm(t("chat.concurrentWarning"))) {
            textarea.value = prompt;
            return;
          }
          request.confirmConcurrent = true;
        }
      }
      const workspaceIndex = workspaceIndexById(workspace.id);
      if (workspaceIndex >= 0) {
        const sessions = [...(workspace.sessions || [])];
        const existingIndex = sessions.findIndex((item) => item.id === result.session.id);
        if (existingIndex >= 0) sessions[existingIndex] = result.session;
        else sessions.unshift(result.session);
        state.workspaces[workspaceIndex] = { ...workspace, locked: true, sessions };
        state.selectedSession = Math.max(0, sessions.findIndex((item) => item.id === result.session.id));
      }
      state.chatLastEventIds[result.session.id] = sessionEventCursor(result.session);
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
    const mode = String(form.get("mode") || "micu");
    const payload = {
      mode,
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
        providerMode: result.settings.mode,
        budget: result.settings.budget,
        codex: {
          mode: result.settings.mode,
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

  document.querySelector("#codex-settings-form").addEventListener("change", (event) => {
    if (!event.target.matches('[name="mode"]')) return;
    document.querySelector("#codex-custom-fields").classList.toggle("hidden", event.target.value !== "custom");
  });
}

function bindInputs() {
  document.addEventListener("input", (event) => {
    if (event.target.matches("[data-name-editor]") && state.nameEditor) state.nameEditor.draft = event.target.value;
  });

  document.addEventListener("focusout", (event) => {
    if (event.target.matches("[data-name-editor]")) commitNameEdit();
  });

  document.addEventListener("keydown", (event) => {
    if (event.target.matches("[data-name-editor]")) {
      if (event.key === "Enter") {
        event.preventDefault();
        commitNameEdit();
      } else if (event.key === "Escape") {
        event.preventDefault();
        cancelNameEdit();
      }
      return;
    }
    if (event.key === "Escape") {
      closePreviewModal();
      closeWorkspaceModal();
      closeCodexSettingsModal();
      closeBatchAccountModal();
      closeRechargeQrModal();
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
    if (event.target.checked) {
      [...state.selectedArtifacts]
        .filter((selected) => selected === path || selected.startsWith(`${path}/`))
        .forEach((selected) => state.selectedArtifacts.delete(selected));
      state.selectedArtifacts.add(path);
    } else {
      deselectTreePath(workspace, path);
    }
    renderArtifacts();
  });

  document.querySelector("#select-all-artifacts").addEventListener("change", (event) => {
    const workspace = safeCurrentWorkspace();
    state.selectedArtifacts = event.target.checked
      ? new Set(
          (workspace?.files || [])
            .filter((file) => Number(file.level || 0) === 0 && (file.type === "file" || file.hasChildren))
            .map((file) => file.path),
        )
      : new Set();
    renderArtifacts();
  });

  document.querySelector("#workspace-run-lock-toggle").addEventListener("change", (event) => {
    updateWorkspaceRunLock(event.target.checked);
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
  await loadRuntimeConfig();
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
  const requestedView = viewFromLocation();
  const view = requestedView === "admin" && state.user?.role !== "system_admin" ? "workspace" : requestedView;
  if (view !== requestedView) routeToView(view, { replace: true });
  switchView(view);
  if (view === "admin" && state.user) loadAccountControlData();
}

window.addEventListener("hashchange", syncRouteFromLocation);
window.addEventListener("popstate", syncRouteFromLocation);
window.setInterval(refreshWorkerStatus, 5_000);

bindGlobalClicks();
bindForms();
bindInputs();
window.aiAuditAppReady = true;
bootstrap();
