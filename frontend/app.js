import { createChatStream } from "./js/appChatStream.js";
import { createAdminRecharge } from "./js/appAdminRecharge.js";
import { createWorkspaceActions } from "./js/appWorkspaceActions.js";
import { createEventBindings } from "./js/appEventBindings.js";
import { createUploadActions } from "./js/appUploads.js";
import { api, apiUrl, authenticatedApiUrl, uploadChunkApi } from "./js/api.js";
import {
  LIVE_CHAT_STATES,
  TERMINAL_CHAT_STATES,
  findSessionById,
  initializeEventCursors,
  mergeHistoricalEvents,
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
  renderRechargeHistory,
  renderWorkspaceManagement,
  renderWorkers,
  renderWorkspaces,
} from "./js/render.js";
import { currentWorkspace } from "./js/selectors.js";
import { isEventStreamNearTop } from "./js/scrollPosition.js";
import { canManageAccounts, isSystemAdmin, applyAdminPermissions, accountSettingsPayload, groupSettingsPayload } from "./js/adminPermissions.js";
import { renderPaymentNotice } from "./js/paymentNotice.js";

const VALID_VIEWS = new Set(["workspace", "chat", "recharge", "admin"]);

export function renderDynamic() {
  renderCurrentUser();
  renderWorkspaces();
  renderWorkspaceManagement();
  renderChatSessions();
  renderEvents();
  renderArtifacts();
  renderFileExplorer();
  renderOperationProgress();
  renderRechargeHistory();
  if (state.rechargeOrder) renderRechargeOrderStatus(state.rechargeOrder);
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

function sortTreeFiles(items) {
  const childrenByParent = new Map();
  items.forEach((item) => {
    const separator = item.path.lastIndexOf("/");
    const parent = separator < 0 ? "" : item.path.slice(0, separator);
    if (!childrenByParent.has(parent)) childrenByParent.set(parent, []);
    childrenByParent.get(parent).push(item);
  });
  const ordered = [];
  const seen = new Set();
  const appendChildren = (parent) => {
    (childrenByParent.get(parent) || []).forEach((item) => {
      if (seen.has(item.path)) return;
      seen.add(item.path);
      ordered.push(item);
      if (item.type === "folder") appendChildren(item.path);
    });
  };
  appendChildren("");
  items.filter((item) => !seen.has(item.path)).forEach((item) => ordered.push(item));
  return ordered;
}

function replaceWorkspace(updatedWorkspace) {
  const index = workspaceIndexById(updatedWorkspace.id);
  if (index < 0) return;
  const current = state.workspaces[index];
  const currentSessions = new Map((current?.sessions || []).map((session) => [session.id, session]));
  const sessions = (updatedWorkspace.sessions || []).map((session) => {
    const existing = currentSessions.get(session.id);
    return {
      ...existing,
      ...session,
      events: session.events?.length ? session.events : (existing?.events || []),
      historyLoaded: Boolean(existing?.historyLoaded),
    };
  });
  updatedWorkspace = { ...updatedWorkspace, sessions };
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
  if (!workspaceId) return null;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}`);
    state.resourceStatus = result.resources || state.resourceStatus;
    initializeEventCursors([result.workspace], state.chatLastEventIds);
    replaceWorkspace(result.workspace);
    renderDynamic();
    return result.workspace;
  } catch (error) {
    showToast(error.message || t("toast.workspaceLoadFailed"));
    return null;
  }
}

async function loadWorkspaces() {
  try {
    const result = await api("/api/workspaces");
    state.workspaces = result.workspaces || [];
    state.resourceStatus = result.resources || state.resourceStatus;
    initializeEventCursors(state.workspaces, state.chatLastEventIds);
    state.workspacesLoaded = true;
    state.selectedWorkspace = Math.min(state.selectedWorkspace, Math.max(state.workspaces.length - 1, 0));
    state.selectedSession = 0;
    const workspaceId = safeCurrentWorkspace()?.id;
    if (workspaceId) await refreshWorkspaceById(workspaceId);
    resetFileTreeState(safeCurrentWorkspace());
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
  if (nextView === "recharge" && state.user) {
    if (!state.recharge) loadRechargeData();
    else refreshCurrentUser().then(renderRechargeHistory);
  }
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
  const isAdmin = canManageAccounts(user);
  applyAdminPermissions(document, user);
  document.querySelector("#admin-nav-button").classList.toggle("hidden", !isAdmin);
  if (!isAdmin && viewFromLocation() === "admin") routeToView("workspace", { replace: true });
  document.querySelector("#auth-screen").classList.add("hidden");
  document.querySelector("#app-shell").classList.remove("hidden");
  renderDynamic();
  if (viewFromLocation() === "recharge" && !state.recharge) loadRechargeData();
}

function showLogin() {
  state.user = null;
  state.workspaceQuery = "";
  document.querySelector("#workspace-search").value = "";
  state.recharge = null;
  state.rechargeImport = null;
  document.querySelector("#recharge-base-url").value = "";
  const rechargeKey = document.querySelector("#recharge-api-key");
  rechargeKey.value = "";
  rechargeKey.type = "password";
  document.querySelector("#recharge-key-visibility").textContent = t("recharge.show");
  document.querySelector("#recharge-import-modal").classList.add("hidden");
  document.querySelector("#auth-screen").classList.remove("hidden");
  document.querySelector("#app-shell").classList.add("hidden");
  setAuthMode("login");
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
  const view = requestedView === "admin" && !canManageAccounts(state.user) ? "workspace" : requestedView;
  if (view !== requestedView) routeToView(view, { replace: true });
  switchView(view);
  if (view === "admin" && state.user) loadAccountControlData();
}

const appContext = {
  api,
  apiUrl,
  authenticatedApiUrl,
  uploadChunkApi,
  LIVE_CHAT_STATES,
  TERMINAL_CHAT_STATES,
  findSessionById,
  initializeEventCursors,
  mergeHistoricalEvents,
  mergeSessionEvents,
  sessionEventCursor,
  applyLocale,
  t,
  state,
  renderAdmin,
  renderArtifacts,
  renderChatSessions,
  renderCurrentUser,
  renderEvents,
  renderFileExplorer,
  renderOperationProgress,
  renderPanelState,
  renderRechargeHistory,
  renderWorkspaceManagement,
  renderWorkers,
  renderWorkspaces,
  currentWorkspace,
  isEventStreamNearTop,
  canManageAccounts,
  isSystemAdmin,
  applyAdminPermissions,
  accountSettingsPayload,
  groupSettingsPayload,
  renderPaymentNotice,
  VALID_VIEWS,
  renderDynamic,
  showToast,
  safeCurrentWorkspace,
  clamp,
  escapeMarkup,
  sortTreeFiles,
  replaceWorkspace,
  resetFileTreeState,
  mergeWorkspaceTreeFiles,
  expandWorkspaceFolder,
  refreshCurrentUser,
  nameEditorElement,
  beginNameEdit,
  cancelNameEdit,
  commitNameEdit,
  setOperationProgress,
  clearOperationProgress,
  refreshWorkspaceById,
  loadWorkspaces,
  startNativeDownload,
  viewFromLocation,
  switchView,
  routeToView,
  showAuthenticated,
  showLogin,
  workspaceAt,
  openWorkspace,
  forkWorkspace,
  createWorkspaceFromModal,
  downloadCurrentWorkspace,
  fileUrl,
  workspaceFileDownloadUrl,
  selectedWorkspaceFilePaths,
  directTreeChildren,
  deselectTreePath,
  openFilePreview,
  toggleWorkspaceSharing,
  updateWorkspaceRunLock,
  uploadFilesToCurrentWorkspace,
  deleteCurrentWorkspacePath,
  deleteSelectedWorkspacePaths,
  deleteWorkspace,
  bootstrap,
  syncRouteFromLocation,
};
Object.assign(appContext, createChatStream(appContext));
Object.assign(appContext, createAdminRecharge(appContext));
Object.assign(appContext, createWorkspaceActions(appContext));
Object.assign(appContext, createEventBindings(appContext));
Object.assign(appContext, createUploadActions(appContext));
const {
  stopChatStream,
  currentSessionObject,
  chatPollInterval,
  scheduleChatPoll,
  pollChatEvents,
  markStreamHealthy,
  scheduleStreamReconnect,
  activatePollingFallback,
  openChatStream,
  startChatStreamForSession,
  loadLatestSessionHistory,
  loadOlderSessionHistory,
  maybeStartChatStream,
  loadAccountControlData,
  refreshWorkerStatus,
  loadRuntimeConfig,
  setAuthMode,
  setGroupMode,
  renderGroupOptions,
  renderCreatedInvites,
  openBatchAccountModal,
  closeBatchAccountModal,
  copyText,
  loadRechargeData,
  rechargeImportStatus,
  renderRechargeImport,
  closeRechargeImport,
  previewRechargeImport,
  applyRechargeImport,
  closeRechargeQrModal,
  renderRechargeOrderStatus,
  pollRechargeOrder,
  openRechargeQrModal,
  revokeInvite,
  openAccountSettingsModal,
  closeAccountSettingsModal,
  openGroupSettingsModal,
  closeGroupSettingsModal,
  createAdminGroup,
  deleteAdminAccount,
  deleteAdminGroup,
  selectWorkspace,
  workspaceIndexById,
  resolveWorkspaceIndex,
  selectWorkspaceFromElement,
  openWorkspaceModal,
  closeWorkspaceModal,
  closeCodexSettingsModal,
  openCodexSettingsModal,
  closePreviewModal,
  closeFileContextMenu,
  uniqueTopFolder,
  addUploadFiles,
  renderSelectedUploadFiles,
  copySession,
  deleteSession,
  createNewChatSession,
  bindGlobalClicks,
  bindForms,
  bindInputs,
  uploadFingerprint,
  runResumableUpload,
} = appContext;
window.addEventListener("hashchange", syncRouteFromLocation);
window.addEventListener("popstate", syncRouteFromLocation);
window.setInterval(refreshWorkerStatus, 5_000);

bindGlobalClicks();
bindForms();
bindInputs();
window.aiAuditAppReady = true;
bootstrap();
