import { t } from "./i18n.js";
import { fallbackAuditEvents } from "./mockData.js";
import { currentSession, currentWorkspace, workspaceList } from "./selectors.js";
import { isEventStreamNearBottom } from "./scrollPosition.js";
import { state } from "./state.js";

const ACTIVE_CHAT_STATES = new Set(["queued", "starting", "running", "stopping"]);
let renderedEventSessionId = "";
let renderedEventSignature = "";

function captureEventScroll(stream) {
  const top = stream.getBoundingClientRect().top;
  const anchor = [...stream.querySelectorAll("[data-event-id]")].find((node) => node.getBoundingClientRect().bottom > top);
  return {
    follow: isEventStreamNearBottom(stream),
    scrollTop: stream.scrollTop,
    anchorId: anchor?.dataset.eventId || "",
    anchorOffset: anchor ? anchor.getBoundingClientRect().top - top : 0,
  };
}

function restoreEventScroll(stream, position) {
  if (position.follow) {
    stream.scrollTop = stream.scrollHeight;
    return;
  }
  const anchor = position.anchorId ? stream.querySelector(`[data-event-id="${position.anchorId}"]`) : null;
  if (anchor) {
    stream.scrollTop += anchor.getBoundingClientRect().top - stream.getBoundingClientRect().top - position.anchorOffset;
  } else {
    stream.scrollTop = position.scrollTop;
  }
}

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

function safeClass(value) {
  return String(value).replace(/[^a-z0-9_-]/gi, "-");
}

function formatResourceBytes(value) {
  let size = Math.max(0, Number(value || 0));
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${index ? size.toFixed(size >= 10 ? 1 : 2) : Math.round(size)} ${units[index]}`;
}

function canRenameWorkspace(workspace) {
  return Boolean(workspace && state.user && (state.user.role === "system_admin" || workspace.owner === state.user.username));
}

function editableName(kind, id, value, location, editable = true) {
  const editor = state.nameEditor;
  const safeId = escapeHtml(id || "");
  const safeLocation = escapeHtml(location);
  if (editor?.kind === kind && editor.id === id && editor.location === location) {
    return `<input class="inline-name-input" data-name-editor data-name-kind="${kind}" data-name-id="${safeId}" data-editor-location="${safeLocation}" value="${escapeHtml(editor.draft)}" maxlength="200" aria-label="${t("actions.rename")}" />`;
  }
  if (!editable) return `<span class="inline-name-static">${escapeHtml(value)}</span>`;
  return `<button type="button" class="inline-name" data-edit-name data-name-kind="${kind}" data-name-id="${safeId}" data-editor-location="${safeLocation}" title="${t("actions.rename")}">${escapeHtml(value)}</button>`;
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function markdownToHtml(value) {
  const blocks = String(value).split(/```/);
  return blocks
    .map((block, index) => {
      if (index % 2 === 1) {
        const lines = block.replace(/^\w+\n/, "").replace(/\n$/, "");
        return `<pre><code>${escapeHtml(lines)}</code></pre>`;
      }
      const lines = block.split(/\n/);
      const html = [];
      let list = [];
      const flushList = () => {
        if (!list.length) return;
        html.push(`<ul>${list.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`);
        list = [];
      };
      lines.forEach((line) => {
        const listMatch = line.match(/^\s*[-*]\s+(.+)$/);
        if (listMatch) {
          list.push(listMatch[1]);
          return;
        }
        flushList();
        if (!line.trim()) return;
        const heading = line.match(/^(#{1,3})\s+(.+)$/);
        if (heading) {
          const level = heading[1].length + 2;
          html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
          return;
        }
        html.push(`<p>${inlineMarkdown(line)}</p>`);
      });
      flushList();
      return html.join("");
    })
    .join("");
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
  const budget = Math.max(0, Number(state.user.budgetTokens || 0));
  const used = Math.max(0, Number(state.user.usedTokens || 0));
  const remaining = Math.max(0, budget - used);
  const percent = budget ? Math.round((used / budget) * 100) : 0;
  document.querySelector("#current-username").textContent = state.user.username;
  document.querySelector("#current-budget-label").textContent = `${remaining.toLocaleString()} / ${budget.toLocaleString()}`;
  document.querySelector("#current-budget-label").title = `${used.toLocaleString()} ${t("session.budgetUsed")} (${percent}%)`;
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
            <h3>${editableName("workspace", workspace.id, workspace.name, `workspace-list:${workspace.id}`, canRenameWorkspace(workspace))}</h3>
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
  document.querySelector("#workspace-management-title").innerHTML = workspace
    ? editableName("workspace", workspace.id, workspace.name, `workspace-management:${workspace.id}`, canRenameWorkspace(workspace))
    : escapeHtml(t("workspace.title"));
  const setting = document.querySelector("#workspace-run-lock-setting");
  const toggle = document.querySelector("#workspace-run-lock-toggle");
  const help = document.querySelector("#workspace-run-lock-help");
  if (!setting || !toggle || !help) return;
  setting.classList.toggle("hidden", !workspace);
  toggle.checked = Boolean(workspace?.runLockEnabled);
  toggle.disabled = !workspace || workspace.owner !== state.user?.username || Boolean(workspace.locked);
  help.textContent = workspace?.locked ? t("workspace.exclusiveRunLockBusy") : t("workspace.exclusiveRunLockHelp");
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
    document.querySelector("#chat-workspace-name").innerHTML = workspace
      ? editableName("workspace", workspace.id, workspace.name, `chat-workspace:${workspace.id}`, canRenameWorkspace(workspace))
      : "";
    document.querySelector("#chat-session-title").textContent = "";
    document.querySelector("#token-count").textContent = "0";
    document.querySelector("#run-state-label").textContent = statusLabel("stopped");
    document.querySelector("#chat-worker-id").textContent = "-";
    document.querySelector("#chat-container-id").textContent = "-";
    document.querySelector("#chat-lock-label").textContent = "-";
    document.querySelector("#chat-cpu-label").textContent = "CPU 0 / 0";
    document.querySelector("#chat-memory-label").textContent = `${t("chat.memory")} 0 / 0`;
    document.querySelector("#chat-cpu-meter").style.width = "0%";
    document.querySelector("#chat-memory-meter").style.width = "0%";
    document.querySelector("#chat-session-list").innerHTML = "";
    return;
  }
  document.querySelector("#chat-workspace-name").innerHTML = editableName("workspace", workspace.id, workspace.name, `chat-workspace:${workspace.id}`, canRenameWorkspace(workspace));
  document.querySelector("#chat-session-title").innerHTML = editableName("session", session.id, session.title, `chat-session-current:${session.id}`);
  document.querySelector("#token-count").textContent = session.tokens;
  document.querySelector("#run-state-label").textContent = statusLabel(session.status);
  document.querySelector("#chat-worker-id").textContent = session.workerId || "-";
  document.querySelector("#chat-container-id").textContent = session.container || "-";
  document.querySelector("#chat-lock-label").textContent = workspace.locked ? t("chat.locked") : t("chat.unlocked");
  const resources = session.resources || {};
  const cpuTotal = Number(resources.cpuTotal || 0);
  const cpuAvailable = Number(resources.cpuAvailable || 0);
  const memoryTotal = Number(resources.memoryTotalBytes || 0);
  const memoryAvailable = Number(resources.memoryAvailableBytes || 0);
  document.querySelector("#chat-cpu-label").textContent = `CPU ${cpuAvailable.toLocaleString()} / ${cpuTotal.toLocaleString()}`;
  document.querySelector("#chat-memory-label").textContent = `${t("chat.memory")} ${formatResourceBytes(memoryAvailable)} / ${formatResourceBytes(memoryTotal)}`;
  document.querySelector("#chat-cpu-meter").style.width = `${cpuTotal ? Math.max(0, Math.min(100, (cpuAvailable / cpuTotal) * 100)) : 0}%`;
  document.querySelector("#chat-memory-meter").style.width = `${memoryTotal ? Math.max(0, Math.min(100, (memoryAvailable / memoryTotal) * 100)) : 0}%`;
  document.querySelector(".status-dot").classList.toggle("running", session.status === "running");
  document.querySelector(".status-dot").classList.toggle("stopped", session.status !== "running");
  const canDeleteSessions = canRenameWorkspace(workspace);
  document.querySelector("#chat-session-list").innerHTML = workspace.sessions
    .map(
      (item, index) => `
        <article class="session-item ${index === state.selectedSession ? "active" : ""}">
          <div class="session-main" data-session="${index}">
            <strong>${editableName("session", item.id, item.title, `chat-session-list:${item.id}`)}</strong>
            <span>${statusLabel(item.status)} · ${item.updated}</span>
          </div>
          <div class="session-actions">
            <button class="session-action-btn" data-session-action="copy" data-session-index="${index}">${t("chat.copy")}</button>
            ${canDeleteSessions ? `<button class="session-action-btn" data-session-action="delete" data-session-index="${index}">${t("chat.delete")}</button>` : ""}
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
    renderedEventSessionId = "";
    renderedEventSignature = "";
    return;
  }
  const signature = JSON.stringify([state.lang, session.id, session.status, session.events]);
  if (renderedEventSessionId === session.id && renderedEventSignature === signature) return;
  const switchingSession = renderedEventSessionId !== session.id;
  const scrollPosition = switchingSession ? null : captureEventScroll(stream);
  let previousRunId = "";
  const events = session.events
    .map(([type, zh, en, runId, eventId, status]) => {
      const message = state.lang === "zh" ? zh : en;
      const divider = runId && previousRunId && runId !== previousRunId ? `<div class="run-divider" aria-hidden="true"></div>` : "";
      previousRunId = runId || previousRunId;
      const flattened = String(message).replace(/\s+/g, " ").trim();
      const body =
        type === "assistant"
          ? `<div class="event-markdown">${markdownToHtml(message)}</div>`
          : type === "command"
            ? `<details class="event-fold" data-multiline="${String(message).includes("\n")}"><summary title="${escapeHtml(flattened)}">${escapeHtml(flattened)}</summary><pre>${escapeHtml(message)}</pre></details>`
            : `<p>${escapeHtml(message)}</p>`;
      const statusBadge = status ? `<span class="event-status event-status-${safeClass(status)}">${escapeHtml(t(`chat.${status}`) || status)}</span>` : "";
      return `${divider}<article class="event event-${safeClass(type)}" data-event-id="${Number(eventId) || 0}"><span class="event-type">${escapeHtml(eventTypeLabel(type))}${statusBadge}</span>${body}</article>`;
    })
    .join("");
  const thinking = ACTIVE_CHAT_STATES.has(session.status)
    ? `<article class="event event-running-dots" role="status" aria-label="${escapeHtml(t("chat.thinking"))}"><span class="event-type">${escapeHtml(t("chat.thinking"))}</span><span class="running-dots" aria-hidden="true"><span></span><span></span><span></span></span></article>`
    : "";
  stream.innerHTML = events + thinking;
  stream.querySelectorAll(".event-fold").forEach((fold) => {
    const summary = fold.querySelector("summary");
    const collapsible = fold.dataset.multiline === "true" || summary.scrollWidth > summary.clientWidth;
    fold.classList.toggle("foldable", collapsible);
  });
  renderedEventSessionId = session.id;
  renderedEventSignature = signature;
  if (switchingSession) stream.scrollTop = stream.scrollHeight;
  else restoreEventScroll(stream, scrollPosition);
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
  const formatBytes = (value) => {
    let size = Math.max(0, Number(value || 0));
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    while (size >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    return `${index ? size.toFixed(size >= 10 ? 1 : 2) : Math.round(size)} ${units[index]}`;
  };
  const accountRow = (account) => {
    const status = account.status === "active" && !account.enabled ? "disabled" : account.status || "active";
    const name = account.username || account.id;
    const token = account.inviteToken || "";
    const isCurrentUser = account.id === state.user?.id || account.username === state.user?.username;
    const actions = [
      token ? `<button class="btn" data-copy-invite="${escapeHtml(token)}">${t("admin.copy")}</button>` : "",
      token ? `<button class="btn" data-revoke-invite="${escapeHtml(account.id)}">${t("admin.revoke")}</button>` : "",
      account.username ? `<button class="btn" data-reset-budget="${escapeHtml(account.id)}">${t("admin.resetBudget")}</button>` : "",
      `<button class="btn danger" data-delete-account="${escapeHtml(account.id)}" ${isCurrentUser ? "disabled" : ""}>${t("admin.deleteUser")}</button>`,
    ].join("");
    return `
      <article class="admin-item admin-user-item">
        <div>
          <h3>${escapeHtml(name)}</h3>
          <div class="meta-line">
            <span>${escapeHtml(account.role || "user")}</span>
            <span class="pill">${escapeHtml(t(`admin.${status}`) || status)}</span>
            <span>${Number(account.usedTokens || 0).toLocaleString()} / ${Number(account.budgetTokens || 0).toLocaleString()} ${t("admin.tokens")}</span>
            <span>${formatBytes(account.diskUsageBytes)} · ${Number(account.workspaceCount || 0)} ${t("admin.workspaces")}</span>
          </div>
          ${token ? `<code class="invite-token">${escapeHtml(token)}</code>` : ""}
        </div>
        <div class="admin-actions">${actions}</div>
      </article>`;
  };
  const groups = state.groups.map((group) => {
    const accounts = state.accounts.filter((account) => account.groupId === group.id);
    const containsCurrentUser = accounts.some((account) => account.id === state.user?.id || account.username === state.user?.username);
    const used = Number(group.diskUsageBytes || 0);
    const limit = group.diskLimitBytes === null || group.diskLimitBytes === undefined ? null : Number(group.diskLimitBytes);
    const percent = limit === null ? 0 : limit === 0 ? (used > 0 ? 100 : 0) : Math.min(100, Math.round((used / limit) * 100));
    const usage = limit === null ? `${formatBytes(used)} / ${t("admin.unlimited")}` : `${formatBytes(used)} / ${formatBytes(limit)}`;
    return `
      <details class="admin-group" open>
        <summary>
          <div>
            <h3>${escapeHtml(group.name)}</h3>
            <div class="meta-line"><span>${Number(group.userCount || 0)} ${t("admin.usersCount")}</span><span>${Number(group.workspaceCount || 0)} ${t("admin.workspaces")}</span><span>${usage}</span></div>
          </div>
          <div class="admin-actions">
            <button class="btn" data-set-group-limit="${escapeHtml(group.id)}">${t("admin.setDiskLimit")}</button>
            <button class="btn danger" data-delete-group="${escapeHtml(group.id)}" ${containsCurrentUser ? "disabled" : ""}>${t("admin.deleteGroup")}</button>
          </div>
        </summary>
        <div class="meter ${limit !== null && used > limit ? "danger" : ""}"><span style="width: ${percent}%"></span></div>
        <div class="admin-group-users">${accounts.length ? accounts.map(accountRow).join("") : `<p class="admin-empty">${t("admin.emptyGroup")}</p>`}</div>
      </details>`;
  });
  const ungrouped = state.accounts.filter((account) => !state.groups.some((group) => group.id === account.groupId));
  if (ungrouped.length) {
    groups.push(`<section class="admin-group"><h3>${t("admin.ungrouped")}</h3>${ungrouped.map(accountRow).join("")}</section>`);
  }
  document.querySelector("#admin-grid").innerHTML = groups.length ? groups.join("") : `<p class="admin-empty">${t("admin.noGroups")}</p>`;

  const logs = state.auditLogs.length
    ? state.auditLogs.map((log) => `${log.actor} ${log.event}${log.detail ? ` - ${log.detail}` : ""}`)
    : fallbackAuditEvents;
  document.querySelector("#audit-list").innerHTML = logs
    .map((event, index) => `<li>${event}<div class="meta-line">${state.auditLogs[index]?.time || `2026-09-12 23:${String(index + 12).padStart(2, "0")}`}</div></li>`)
    .join("");
}

export function renderWorkers() {
  const grid = document.querySelector("#worker-grid");
  if (!grid) return;
  if (!state.workers.length) {
    grid.innerHTML = `<p class="admin-empty">${t("admin.noWorkers")}</p>`;
    return;
  }
  grid.innerHTML = state.workers
    .map((worker) => {
      const cpuTotal = Number(worker.cpuTotal || 0);
      const cpuAvailable = Number(worker.cpuAvailable || 0);
      const memoryTotal = Number(worker.memoryTotalBytes || 0);
      const memoryAvailable = Number(worker.memoryAvailableBytes || 0);
      const cpuPercent = cpuTotal ? Math.max(0, Math.min(100, (cpuAvailable / cpuTotal) * 100)) : 0;
      const memoryPercent = memoryTotal ? Math.max(0, Math.min(100, (memoryAvailable / memoryTotal) * 100)) : 0;
      return `<article class="worker-card">
        <div class="worker-card-head"><h3>${escapeHtml(worker.id)}</h3><span class="pill ${worker.healthy ? "" : "danger"}">${t(worker.enabled === false ? "admin.workerDisabled" : worker.healthy ? "admin.workerHealthy" : "admin.workerUnhealthy")}</span></div>
        <div class="meta-line"><span>${escapeHtml(worker.ip || "-")}${worker.port ? `:${Number(worker.port)}` : ""}</span><span>${Number(worker.activeRunCount || 0)} ${t("admin.activeRuns")} · ${Number(worker.activeUploadCount || 0)} ${t("admin.activeUploads")}</span></div>
        <div class="worker-resource"><span>CPU ${cpuAvailable.toLocaleString()} / ${cpuTotal.toLocaleString()}</span><div class="meter"><span style="width:${cpuPercent}%"></span></div></div>
        <div class="worker-resource"><span>${t("chat.memory")} ${formatResourceBytes(memoryAvailable)} / ${formatResourceBytes(memoryTotal)}</span><div class="meter green"><span style="width:${memoryPercent}%"></span></div></div>
        <div class="meta-line"><span>${t("admin.lastContact")}: ${escapeHtml(worker.lastContact || "-")}</span>${worker.error ? `<span>${escapeHtml(worker.error)}</span>` : ""}</div>
      </article>`;
    })
    .join("");
}
