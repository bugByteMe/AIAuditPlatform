import { manageModalFocus } from "./modalFocus.js";

// Event delegation and form/input handlers for the application.
export function createEventBindings(context) {
  let registrationSubmitting = false;
  const api = (...args) => context.api(...args);
  const sessionEventCursor = (...args) => context.sessionEventCursor(...args);
  const applyLocale = (...args) => context.applyLocale(...args);
  const t = (...args) => context.t(...args);
  const state = context.state;
  const renderArtifacts = (...args) => context.renderArtifacts(...args);
  const renderCurrentUser = (...args) => context.renderCurrentUser(...args);
  const renderFileExplorer = (...args) => context.renderFileExplorer(...args);
  const renderPanelState = (...args) => context.renderPanelState(...args);
  const renderWorkspaces = (...args) => context.renderWorkspaces(...args);
  const isEventStreamNearTop = (...args) => context.isEventStreamNearTop(...args);
  const canManageAccounts = (...args) => context.canManageAccounts(...args);
  const isSystemAdmin = (...args) => context.isSystemAdmin(...args);
  const accountSettingsPayload = (...args) => context.accountSettingsPayload(...args);
  const groupSettingsPayload = (...args) => context.groupSettingsPayload(...args);
  const renderDynamic = (...args) => context.renderDynamic(...args);
  const showToast = (...args) => context.showToast(...args);
  const safeCurrentWorkspace = (...args) => context.safeCurrentWorkspace(...args);
  const clamp = (...args) => context.clamp(...args);
  const expandWorkspaceFolder = (...args) => context.expandWorkspaceFolder(...args);
  const beginNameEdit = (...args) => context.beginNameEdit(...args);
  const cancelNameEdit = (...args) => context.cancelNameEdit(...args);
  const commitNameEdit = (...args) => context.commitNameEdit(...args);
  const currentSessionObject = (...args) => context.currentSessionObject(...args);
  const startChatStreamForSession = (...args) => context.startChatStreamForSession(...args);
  const loadOlderSessionHistory = (...args) => context.loadOlderSessionHistory(...args);
  const maybeStartChatStream = (...args) => context.maybeStartChatStream(...args);
  const loadWorkspaces = (...args) => context.loadWorkspaces(...args);
  const startNativeDownload = (...args) => context.startNativeDownload(...args);
  const routeToView = (...args) => context.routeToView(...args);
  const showAuthenticated = (...args) => context.showAuthenticated(...args);
  const showLogin = (...args) => context.showLogin(...args);
  const loadAccountControlData = (...args) => context.loadAccountControlData(...args);
  const setAuthMode = (...args) => context.setAuthMode(...args);
  const setGroupMode = (...args) => context.setGroupMode(...args);
  const renderGroupOptions = (...args) => context.renderGroupOptions(...args);
  const renderCreatedInvites = (...args) => context.renderCreatedInvites(...args);
  const openBatchAccountModal = (...args) => context.openBatchAccountModal(...args);
  const closeBatchAccountModal = (...args) => context.closeBatchAccountModal(...args);
  const copyText = (...args) => context.copyText(...args);
  const renderRechargeImport = (...args) => context.renderRechargeImport(...args);
  const closeRechargeImport = (...args) => context.closeRechargeImport(...args);
  const previewRechargeImport = (...args) => context.previewRechargeImport(...args);
  const applyRechargeImport = (...args) => context.applyRechargeImport(...args);
  const closeRechargeQrModal = (...args) => context.closeRechargeQrModal(...args);
  const openRechargeQrModal = (...args) => context.openRechargeQrModal(...args);
  const revokeInvite = (...args) => context.revokeInvite(...args);
  const openAccountSettingsModal = (...args) => context.openAccountSettingsModal(...args);
  const closeAccountSettingsModal = (...args) => context.closeAccountSettingsModal(...args);
  const openGroupSettingsModal = (...args) => context.openGroupSettingsModal(...args);
  const closeGroupSettingsModal = (...args) => context.closeGroupSettingsModal(...args);
  const createAdminGroup = (...args) => context.createAdminGroup(...args);
  const deleteAdminAccount = (...args) => context.deleteAdminAccount(...args);
  const deleteAdminGroup = (...args) => context.deleteAdminGroup(...args);
  const workspaceIndexById = (...args) => context.workspaceIndexById(...args);
  const resolveWorkspaceIndex = (...args) => context.resolveWorkspaceIndex(...args);
  const selectWorkspaceFromElement = (...args) => context.selectWorkspaceFromElement(...args);
  const openWorkspaceModal = (...args) => context.openWorkspaceModal(...args);
  const closeWorkspaceModal = (...args) => context.closeWorkspaceModal(...args);
  const closeCodexSettingsModal = (...args) => context.closeCodexSettingsModal(...args);
  const openCodexSettingsModal = (...args) => context.openCodexSettingsModal(...args);
  const closePreviewModal = (...args) => context.closePreviewModal(...args);
  const closeFileContextMenu = (...args) => context.closeFileContextMenu(...args);
  const addUploadFiles = (...args) => context.addUploadFiles(...args);
  const copySession = (...args) => context.copySession(...args);
  const deleteSession = (...args) => context.deleteSession(...args);
  const createNewChatSession = (...args) => context.createNewChatSession(...args);
  const workspaceAt = (...args) => context.workspaceAt(...args);
  const openWorkspace = (...args) => context.openWorkspace(...args);
  const forkWorkspace = (...args) => context.forkWorkspace(...args);
  const createWorkspaceFromModal = (...args) => context.createWorkspaceFromModal(...args);
  const downloadCurrentWorkspace = (...args) => context.downloadCurrentWorkspace(...args);
  const workspaceFileDownloadUrl = (...args) => context.workspaceFileDownloadUrl(...args);
  const deselectTreePath = (...args) => context.deselectTreePath(...args);
  const openFilePreview = (...args) => context.openFilePreview(...args);
  const toggleWorkspaceSharing = (...args) => context.toggleWorkspaceSharing(...args);
  const updateWorkspaceRunLock = (...args) => context.updateWorkspaceRunLock(...args);
  const uploadFilesToCurrentWorkspace = (...args) => context.uploadFilesToCurrentWorkspace(...args);
  const deleteCurrentWorkspacePath = (...args) => context.deleteCurrentWorkspacePath(...args);
  const deleteSelectedWorkspacePaths = (...args) => context.deleteSelectedWorkspacePaths(...args);
  const deleteWorkspace = (...args) => context.deleteWorkspace(...args);

  function bindGlobalClicks() {
    document.addEventListener("click", (event) => {
      document.querySelectorAll(".action-menu[open]").forEach((menu) => {
        if (!menu.contains(event.target) || event.target.closest(".action-menu-items button")) menu.open = false;
      });
      if (event.target.closest("[data-retry-chat-history]")) {
        const session = currentSessionObject();
        if (session) {
          session.historyLoadError = false;
          if (session.historyLoaded) loadOlderSessionHistory();
          else maybeStartChatStream();
        }
        return;
      }
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

      if (event.target.closest("#recharge-import-button")) {
        document.querySelector("#recharge-import-input").click();
        return;
      }

      if (event.target.closest("#recharge-import-confirm")) {
        applyRechargeImport();
        return;
      }

      if (event.target.closest("[data-close-recharge-import]")) {
        closeRechargeImport();
        return;
      }

      if (event.target.closest("#create-group-button")) {
        createAdminGroup();
        return;
      }

      const accountSettings = event.target.closest("[data-open-account-settings]");
      if (accountSettings) {
        openAccountSettingsModal(accountSettings.dataset.openAccountSettings);
        return;
      }

      const groupSettings = event.target.closest("[data-open-group-settings]");
      if (groupSettings) {
        event.preventDefault();
        openGroupSettingsModal(groupSettings.dataset.openGroupSettings);
        return;
      }

      if (event.target.closest("[data-close-account-settings]")) {
        closeAccountSettingsModal();
        return;
      }

      if (event.target.closest("[data-close-group-settings]")) {
        closeGroupSettingsModal();
        return;
      }

      if (event.target.closest("#account-settings-delete")) {
        const accountId = document.querySelector('#account-settings-form [name="accountId"]').value;
        if (accountId) deleteAdminAccount(accountId);
        return;
      }

      if (event.target.closest("#group-settings-delete")) {
        const groupId = document.querySelector('#group-settings-form [name="groupId"]').value;
        if (groupId) deleteAdminGroup(groupId);
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
        if (nav.dataset.view === "admin" && !canManageAccounts(state.user)) return;
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
        if (state.rechargeImport) renderRechargeImport();
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

      if (event.target.closest(".action-menu > summary")) return;
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
      if (registrationSubmitting) return;
      registrationSubmitting = true;
      const formElement = event.currentTarget;
      const form = new FormData(formElement);
      const registerError = document.querySelector("#register-error");
      const submitButton = formElement.querySelector('button[type="submit"]');
      const buttonLabel = submitButton?.querySelector(".button-label");
      registerError.textContent = t("auth.registering");
      if (submitButton) {
        submitButton.disabled = true;
        submitButton.classList.add("is-loading");
        submitButton.setAttribute("aria-busy", "true");
      }
      if (buttonLabel) buttonLabel.textContent = t("auth.registering");
      try {
        const result = await api("/api/register", {
          method: "POST",
          body: JSON.stringify({ inviteToken: form.get("inviteToken"), username: form.get("username"), password: form.get("password") }),
        });
        formElement.reset();
        if (!result.sessionToken) {
          setAuthMode("login");
          document.querySelector("#login-error").textContent = result.sessionError === "account_disabled"
            ? t("auth.activationAccountDisabled")
            : t("auth.activationSessionLimit");
          return;
        }
        showAuthenticated(result.user);
        try {
          await loadWorkspaces();
          await loadAccountControlData();
        } catch (error) {
          console.warn("Post-registration loading failed", error);
          showToast(t("toast.workspaceLoadFailed"));
        }
      } catch (error) {
        registerError.textContent = `${t("auth.registerFailed")} ${error.message || ""}`.trim();
      } finally {
        registrationSubmitting = false;
        if (submitButton) {
          submitButton.disabled = false;
          submitButton.classList.remove("is-loading");
          submitButton.removeAttribute("aria-busy");
        }
        if (buttonLabel) buttonLabel.textContent = t("auth.activate");
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

    document.querySelector("#account-settings-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      const accountId = String(form.get("accountId") || "");
      const maxSessions = Number(form.get("maxSessions"));
      const amountCny = Number(form.get("amountCny") || 0);
      const errorElement = document.querySelector("#account-settings-error");
      const submitButton = event.currentTarget.querySelector('button[type="submit"]');
      errorElement.textContent = "";
      if (!accountId || !Number.isInteger(maxSessions) || maxSessions < 0 || !Number.isFinite(amountCny) || amountCny < 0) return;
      submitButton.disabled = true;
      try {
        await api(`/api/accounts/${encodeURIComponent(accountId)}`, {
          method: "PATCH",
          body: JSON.stringify(accountSettingsPayload(state.user, form)),
        });
        const account = state.accounts.find((item) => item.id === accountId);
        if (isSystemAdmin(state.user) && amountCny > 0 && account?.username && account.budget?.source !== "custom") {
          await api(`/api/accounts/${encodeURIComponent(accountId)}/reset-budget`, {
            method: "POST",
            body: JSON.stringify({ amountCny: amountCny.toFixed(2) }),
          });
        }
        const session = await api("/api/session");
        showAuthenticated(session.user);
        await loadAccountControlData();
        closeAccountSettingsModal();
        showToast(t("toast.accountSettingsSaved"));
      } catch (error) {
        errorElement.textContent = error.message;
      } finally {
        submitButton.disabled = false;
      }
    });

    document.querySelector("#group-settings-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      const groupId = String(form.get("groupId") || "");
      const name = String(form.get("name") || "").trim();
      const liveRunLimit = Number(form.get("liveRunLimit"));
      const rawDiskLimit = String(form.get("diskLimitMib") || "").trim();
      const diskLimitMib = rawDiskLimit === "" ? null : Number(rawDiskLimit);
      const errorElement = document.querySelector("#group-settings-error");
      const submitButton = event.currentTarget.querySelector('button[type="submit"]');
      errorElement.textContent = "";
      if (!groupId || !name || !Number.isInteger(liveRunLimit) || liveRunLimit < 0 || (diskLimitMib !== null && (!Number.isFinite(diskLimitMib) || diskLimitMib < 0))) return;
      submitButton.disabled = true;
      try {
        await api(`/api/groups/${encodeURIComponent(groupId)}`, {
          method: "PATCH",
          body: JSON.stringify(groupSettingsPayload(state.user, form)),
        });
        await loadAccountControlData();
        closeGroupSettingsModal();
        showToast(t("toast.groupSettingsSaved"));
      } catch (error) {
        errorElement.textContent = error.message;
      } finally {
        submitButton.disabled = false;
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
          state.workspaces[workspaceIndex] = { ...workspace, locked: true, sessions, sessionCount: sessions.length };
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
    manageModalFocus();
    const composerHandle = document.querySelector("#composer-resize-handle");
    const composerInput = document.querySelector("#composer textarea");
    const resizeComposer = (height) => {
      const maxHeight = Math.max(82, Math.min(320, window.innerHeight * 0.35));
      composerInput.style.height = `${clamp(height, 82, maxHeight)}px`;
    };
    composerHandle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const startY = event.clientY;
      const startHeight = composerInput.getBoundingClientRect().height;
      composerHandle.setPointerCapture(event.pointerId);
      const onMove = (moveEvent) => {
        resizeComposer(startHeight + startY - moveEvent.clientY);
      };
      const onEnd = () => {
        composerHandle.removeEventListener("pointermove", onMove);
        composerHandle.removeEventListener("pointerup", onEnd);
        composerHandle.removeEventListener("pointercancel", onEnd);
      };
      composerHandle.addEventListener("pointermove", onMove);
      composerHandle.addEventListener("pointerup", onEnd);
      composerHandle.addEventListener("pointercancel", onEnd);
    });
    composerHandle.addEventListener("keydown", (event) => {
      const height = composerInput.getBoundingClientRect().height;
      if (event.key === "ArrowUp") resizeComposer(height + 20);
      else if (event.key === "ArrowDown") resizeComposer(height - 20);
      else if (event.key === "Home") resizeComposer(82);
      else if (event.key === "End") resizeComposer(320);
      else return;
      event.preventDefault();
    });
    document.querySelector("#workspace-search").addEventListener("input", (event) => {
      state.workspaceQuery = event.target.value;
      renderWorkspaces();
    });
    document.querySelector("#event-stream").addEventListener("scroll", (event) => {
      if (isEventStreamNearTop(event.currentTarget)) loadOlderSessionHistory();
    }, { passive: true });
    document.querySelector("#recharge-import-input").addEventListener("change", (event) => {
      const file = event.target.files?.[0];
      if (file) previewRechargeImport(file);
    });
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
        document.querySelectorAll(".action-menu[open]").forEach((menu) => { menu.open = false; });
        closePreviewModal();
        closeWorkspaceModal();
        closeCodexSettingsModal();
        closeBatchAccountModal();
        closeRechargeImport();
        closeRechargeQrModal();
        closeFileContextMenu();
        return;
      }
      if (event.target.closest(".action-menu")) return;
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
      const previousStatus = session.status;
      session.status = "stopping";
      renderDynamic();
      try {
        await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/chat/runs/${encodeURIComponent(runId)}/stop`, { method: "POST" });
        startChatStreamForSession(workspace.id, session.id);
        showToast(t("toast.stop"));
      } catch (error) {
        session.status = previousStatus;
        renderDynamic();
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

  return { bindGlobalClicks, bindForms, bindInputs };
}
