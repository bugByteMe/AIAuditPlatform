import { t } from "./i18n.js";
import { fallbackAccounts, fallbackAuditEvents } from "./mockData.js";
import { currentSession, currentWorkspace, workspaceList } from "./selectors.js";
import { state } from "./state.js";

function statusLabel(status) {
  return t(`chat.${status}`) || status;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function eventTypeLabel(type) {
  if (type === "command") return t("chat.command");
  return type;
}

function descendantFilePaths(items, folderPath) {
  return items
    .filter((item) => item.type === "file" && item.path.startsWith(`${folderPath}/`))
    .map((item) => item.path);
}

function treeRows(items, collapsedFolders, options = {}) {
  if (!items.length) return `<div class="file-empty">${options.emptyText || t("files.empty")}</div>`;
  return items
    .filter((file) => {
      const path = file.path || file.name;
      const parts = path.split("/");
      const ancestors = parts.slice(0, file.type === "folder" ? -1 : parts.length - 1);
      return !ancestors.some((_, index) => collapsedFolders.has(ancestors.slice(0, index + 1).join("/")));
    })
    .map((file) => {
      const path = file.path || file.name;
      const safePath = escapeHtml(path);
      const safeName = escapeHtml(file.name);
      const status = file.status ? `<span class="file-status ${file.status}">${t(`artifact.${file.status}`)}</span>` : "";
      const meta = file.type === "folder" ? t("files.folder") : file.size || t("files.file");
      const collapsed = file.type === "folder" && collapsedFolders.has(path);
      const icon = file.type === "folder" ? (collapsed ? "▸" : "▾") : "·";
      const folderAttr = file.type === "folder" ? `data-folder-path="${safePath}"` : "";
      const folderDescendants = file.type === "folder" ? descendantFilePaths(items, path) : [];
      const checkable = options.checkboxes && (file.type === "file" || folderDescendants.length);
      const checked =
        file.type === "folder"
          ? folderDescendants.length > 0 && folderDescendants.every((itemPath) => options.selectedPaths?.has(itemPath))
          : options.selectedPaths?.has(path);
      const checkbox = checkable
        ? `<input type="checkbox" data-artifact="${safePath}" ${file.type === "folder" ? `data-artifact-folder="${safePath}"` : ""} ${checked ? "checked" : ""} ${file.disabled ? "disabled" : ""} />`
          : "";
      const contextAttr = options.context ? `data-context-menu="${options.context}"` : "";
      const treeAttr = options.tree ? `data-tree="${options.tree}"` : "";
      return `
        <div class="file-row ${file.type} ${checkbox ? "selectable" : ""}" style="--level: ${file.level || 0}" title="${safePath}" data-file-path="${safePath}" data-file-type="${file.type}" ${folderAttr} ${contextAttr} ${treeAttr}>
          <span class="file-icon" aria-hidden="true">${icon}</span>
          ${checkbox}
          <span class="file-name">${safeName}</span>
          <span class="file-meta">${meta}</span>
          ${status}
        </div>
      `;
    })
    .join("");
}

function filesToTree(files, collapsedFolders, options = {}) {
  return treeRows(files, collapsedFolders, options);
}

export function renderCurrentUser() {
  if (!state.user) return;
  const percent = state.user.budgetTokens ? Math.round((state.user.usedTokens / state.user.budgetTokens) * 100) : 0;
  document.querySelector("#current-username").textContent = state.user.username;
  document.querySelector("#current-budget-label").textContent = `${percent}%`;
  document.querySelector("#current-budget-meter").style.width = `${Math.min(percent, 100)}%`;
}

export function renderWorkspaces() {
  const items = workspaceList();
  if (!items.length) {
    document.querySelector("#workspace-list").innerHTML = `<div class="empty-state">${state.lang === "zh" ? "还没有工作区，请上传文件夹创建。" : "No workspaces yet. Upload files to create one."}</div>`;
    return;
  }
  document.querySelector("#workspace-list").innerHTML = items
    .map((workspace, index) => {
      const safeWorkspaceId = escapeHtml(workspace.id || "");
      const safeWorkspaceName = escapeHtml(workspace.name);
      const safeOwner = escapeHtml(workspace.owner);
      const safeSize = escapeHtml(workspace.size);
      const safeUpdated = escapeHtml(workspace.updated);
      const visibility = workspace.shared ? t("workspace.group") : t("workspace.private");
      const lock = workspace.locked
        ? `<span class="pill warning">${t("workspace.locked")}</span>`
        : `<span class="pill success">${t("workspace.lockFree")}</span>`;
      const shareTitle = workspace.shared ? t("workspace.unshare") : t("workspace.share");
      const shareIcon = workspace.shared ? "↙" : "↗";
      return `
        <article class="workspace-item ${index === state.selectedWorkspace ? "active" : ""}" data-workspace-card="${index}" data-workspace-id="${safeWorkspaceId}" tabindex="0">
          <div>
            <h3>${safeWorkspaceName}</h3>
            <div class="meta-line">
              <span>${safeOwner}</span>
              <span>${workspace.fileCount.toLocaleString()} files</span>
              <span>${safeSize}</span>
              <span>${safeUpdated}</span>
              <span>${workspace.sessions.length} ${state.lang === "zh" ? "个会话" : "sessions"}</span>
              <span class="pill blue">${visibility}</span>
              ${lock}
            </div>
          </div>
          <div class="toolbar-actions">
            <button type="button" class="btn icon-btn" data-action="share" data-index="${index}" data-workspace-id="${safeWorkspaceId}" aria-label="${shareTitle}" title="${shareTitle}">
              <span aria-hidden="true">${shareIcon}</span>
            </button>
            <button type="button" class="btn icon-btn" data-action="fork" data-index="${index}" data-workspace-id="${safeWorkspaceId}" aria-label="${t("workspace.fork")}" title="${t("workspace.fork")}">
              <span aria-hidden="true">⧉</span>
            </button>
            <button type="button" class="btn icon-btn danger" data-action="delete" data-index="${index}" data-workspace-id="${safeWorkspaceId}" aria-label="${t("workspace.delete")}" title="${t("workspace.delete")}">
              <span aria-hidden="true">🗑</span>
            </button>
            <button type="button" class="btn icon-btn primary" data-action="open" data-index="${index}" data-workspace-id="${safeWorkspaceId}" aria-label="${t("workspace.open")}" title="${t("workspace.open")}">
              <span aria-hidden="true">▶</span>
            </button>
          </div>
        </article>
      `;
    })
    .join("");
}

export function renderWorkspaceManagement() {
  const workspace = currentWorkspace();
  document.querySelector("#workspace-management-title").textContent = workspace ? workspace.name : t("workspace.title");
}

export function renderPanelState() {
  const layout = document.querySelector(".chat-layout");
  if (!layout) return;
  layout.style.setProperty("--sessions-width", `${state.sessionPanelWidth}px`);
  layout.style.setProperty("--files-width", `${state.filePanelWidth}px`);
  layout.classList.toggle("sessions-collapsed", state.isSessionPanelCollapsed);
  layout.classList.toggle("files-collapsed", state.isFileExplorerCollapsed);
  document.querySelectorAll('[data-toggle-panel="sessions"]').forEach((button) => {
    button.dataset.i18nTitle = state.isSessionPanelCollapsed ? "chat.expandSessions" : "chat.collapseSessions";
    button.title = t(button.dataset.i18nTitle);
  });
  document.querySelectorAll('[data-toggle-panel="files"]').forEach((button) => {
    button.dataset.i18nTitle = state.isFileExplorerCollapsed ? "files.expand" : "files.collapse";
    button.title = t(button.dataset.i18nTitle);
  });
}

export function renderChatSessions() {
  const workspace = currentWorkspace();
  const session = currentSession();
  if (!workspace || !session) {
    document.querySelector("#chat-workspace-name").textContent = "";
    document.querySelector("#chat-session-title").textContent = "";
    document.querySelector("#token-count").textContent = "0";
    document.querySelector("#run-state-label").textContent = statusLabel("stopped");
    document.querySelector("#chat-session-list").innerHTML = "";
    return;
  }
  document.querySelector("#chat-workspace-name").textContent = workspace.name;
  document.querySelector("#chat-session-title").textContent = session.title;
  document.querySelector("#token-count").textContent = session.tokens;
  document.querySelector("#run-state-label").textContent = statusLabel(session.status);
  document.querySelector(".status-dot").classList.toggle("running", session.status === "running");
  document.querySelector(".status-dot").classList.toggle("stopped", session.status !== "running");
  document.querySelector("#chat-session-list").innerHTML = workspace.sessions
    .map(
      (item, index) => `
        <article class="session-item ${index === state.selectedSession ? "active" : ""}">
          <button class="session-main" data-session="${index}">
            <strong>${item.title}</strong>
            <span>${statusLabel(item.status)} · ${item.updated}</span>
          </button>
          <div class="session-actions">
            <button class="session-action-btn" data-session-action="copy" data-session-index="${index}">${t("chat.copy")}</button>
            <button class="session-action-btn" data-session-action="delete" data-session-index="${index}">${t("chat.delete")}</button>
          </div>
        </article>
      `,
    )
    .join("");
}

export function renderEvents() {
  const stream = document.querySelector("#event-stream");
  const session = currentSession();
  if (!session) {
    stream.innerHTML = "";
    return;
  }
  let previousRunId = "";
  stream.innerHTML = session.events
    .map(([type, zh, en, runId]) => {
      const message = state.lang === "zh" ? zh : en;
      const divider = runId && previousRunId && runId !== previousRunId ? `<div class="run-divider" aria-hidden="true"></div>` : "";
      previousRunId = runId || previousRunId;
      return `${divider}<article class="event event-${escapeHtml(type)}"><span class="event-type">${escapeHtml(eventTypeLabel(type))}</span><p>${escapeHtml(message)}</p></article>`;
    })
    .join("");
  stream.scrollTop = stream.scrollHeight;
}

export function renderOperationProgress() {
  const progress = state.operationProgress;
  const container = document.querySelector("#operation-progress");
  if (!container) return;
  container.classList.toggle("hidden", !progress);
  if (!progress) return;
  const value = Math.max(0, Math.min(Number(progress.value || 0), 100));
  document.querySelector("#operation-progress-label").textContent = progress.label;
  document.querySelector("#operation-progress-value").textContent = progress.indeterminate ? "" : `${value}%`;
  document.querySelector("#operation-progress-fill").style.width = progress.indeterminate ? "42%" : `${value}%`;
  container.classList.toggle("indeterminate", Boolean(progress.indeterminate));
}

export function renderArtifacts() {
  const workspace = currentWorkspace();
  const tree = document.querySelector("#artifact-tree");
  if (!tree) return;
  const workspaceFiles = (workspace?.files || []).filter((file) => file.type === "file");
  const selectAll = document.querySelector("#select-all-artifacts");
  if (selectAll) {
    const selectedCount = workspaceFiles.filter((file) => state.selectedArtifacts.has(file.path)).length;
    selectAll.checked = workspaceFiles.length > 0 && selectedCount === workspaceFiles.length;
    selectAll.indeterminate = selectedCount > 0 && selectedCount < workspaceFiles.length;
  }
  const artifactStatuses = new Map((workspace?.artifacts || []).map((artifact) => [artifact.path, artifact.status]));
  const files = (workspace?.files || []).map((file) => ({
    ...file,
    status: file.type === "file" ? artifactStatuses.get(file.path) : "",
  }));
  tree.innerHTML = workspace
    ? treeRows(files, state.collapsedArtifactFolders, {
        checkboxes: true,
        context: "artifact-file",
        selectedPaths: state.selectedArtifacts,
        tree: "artifacts",
        emptyText: t("files.empty"),
      })
    : "";
}

export function renderFileExplorer() {
  const workspace = currentWorkspace();
  const files = workspace?.files || [];
  const tree = document.querySelector("#file-tree");
  tree.innerHTML = filesToTree(files, state.collapsedFileFolders, { tree: "chat-files" });
}

export function renderAdmin() {
  const accountRows = state.accounts.length
    ? state.accounts.map((account) => [
        account.username,
        `${account.role}${account.enabled ? "" : " disabled"}`,
        `${Number(account.usedTokens || 0).toLocaleString()} / ${Number(account.budgetTokens || 0).toLocaleString()} tokens`,
      ])
    : fallbackAccounts;
  document.querySelector("#admin-grid").innerHTML = accountRows
    .map(
      ([name, role, budget]) => `
        <article class="admin-item">
          <div><h3>${name}</h3><div class="meta-line"><span>${role}</span><span>${budget}</span></div></div>
          <button class="btn">${state.lang === "zh" ? "调整预算" : "Adjust budget"}</button>
        </article>
      `,
    )
    .join("");

  const logs = state.auditLogs.length
    ? state.auditLogs.map((log) => `${log.actor} ${log.event}${log.detail ? ` - ${log.detail}` : ""}`)
    : fallbackAuditEvents;
  document.querySelector("#audit-list").innerHTML = logs
    .map((event, index) => `<li>${event}<div class="meta-line">${state.auditLogs[index]?.time || `2026-09-12 23:${String(index + 12).padStart(2, "0")}`}</div></li>`)
    .join("");
}
