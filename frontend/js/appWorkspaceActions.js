// Frontend controller: workspaceactions responsibilities.
export function createWorkspaceActions(context) {
  const api = (...args) => context.api(...args);
  const LIVE_CHAT_STATES = context.LIVE_CHAT_STATES;
  const sessionEventCursor = (...args) => context.sessionEventCursor(...args);
  const t = (...args) => context.t(...args);
  const state = context.state;
  const renderDynamic = (...args) => context.renderDynamic(...args);
  const showToast = (...args) => context.showToast(...args);
  const safeCurrentWorkspace = (...args) => context.safeCurrentWorkspace(...args);
  const stopChatStream = (...args) => context.stopChatStream(...args);
  const resetFileTreeState = (...args) => context.resetFileTreeState(...args);
  const refreshWorkspaceById = (...args) => context.refreshWorkspaceById(...args);
  const maybeStartChatStream = (...args) => context.maybeStartChatStream(...args);
  const routeToView = (...args) => context.routeToView(...args);
  const workspaceAt = (...args) => context.workspaceAt(...args);

  async function selectWorkspace(index) {
    state.selectedWorkspace = Number(index);
    state.selectedSession = 0;
    stopChatStream();
    const workspaceId = safeCurrentWorkspace()?.id;
    renderDynamic();
    if (workspaceId && !safeCurrentWorkspace()?.detailLoaded) await refreshWorkspaceById(workspaceId);
    if (safeCurrentWorkspace()?.id !== workspaceId) return;
    resetFileTreeState(safeCurrentWorkspace());
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
      state.workspaces[workspaceIndex] = { ...workspace, sessions, sessionCount: sessions.length };
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
      state.workspaces[workspaceIndex] = { ...latestWorkspace, sessions, sessionCount: sessions.length };
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
        state.workspaces[workspaceIndex] = { ...workspace, sessions, sessionCount: sessions.length };
        state.selectedSession = 0;
      }
      routeToView("chat");
      renderDynamic();
      document.querySelector("#composer textarea")?.focus();
    } catch (error) {
      showToast(error.message);
    }
  }

  return { selectWorkspace, workspaceIndexById, resolveWorkspaceIndex, selectWorkspaceFromElement, openWorkspaceModal, closeWorkspaceModal, closeCodexSettingsModal, openCodexSettingsModal, closePreviewModal, closeFileContextMenu, uniqueTopFolder, addUploadFiles, renderSelectedUploadFiles, copySession, deleteSession, createNewChatSession };
}
