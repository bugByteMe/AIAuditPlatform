// Frontend controller: adminrecharge responsibilities.
export function createAdminRecharge(context) {
  let workerStatusLoading = false;
  let rechargeOrderPollTimer = null;
  let rechargeOrderGeneration = 0;
  const api = (...args) => context.api(...args);
  const authenticatedApiUrl = (...args) => context.authenticatedApiUrl(...args);
  const t = (...args) => context.t(...args);
  const state = context.state;
  const renderAdmin = (...args) => context.renderAdmin(...args);
  const renderRechargeHistory = (...args) => context.renderRechargeHistory(...args);
  const renderWorkers = (...args) => context.renderWorkers(...args);
  const renderWorkspaces = (...args) => context.renderWorkspaces(...args);
  const canManageAccounts = (...args) => context.canManageAccounts(...args);
  const isSystemAdmin = (...args) => context.isSystemAdmin(...args);
  const renderPaymentNotice = (...args) => context.renderPaymentNotice(...args);
  const showToast = (...args) => context.showToast(...args);
  const escapeMarkup = (...args) => context.escapeMarkup(...args);
  const refreshCurrentUser = (...args) => context.refreshCurrentUser(...args);
  const viewFromLocation = (...args) => context.viewFromLocation(...args);

  async function loadAccountControlData() {
    if (!canManageAccounts(state.user)) {
      state.accounts = [];
      state.groups = [];
      state.workers = [];
      renderAdmin();
      renderWorkers();
      return;
    }
    try {
      const [accounts, logs, workers] = await Promise.all([api("/api/accounts"), isSystemAdmin(state.user) ? api("/api/audit-logs") : { logs: [] }, isSystemAdmin(state.user) ? api("/api/workers") : { workers: [] }]);
      state.accounts = accounts.accounts || [];
      state.groups = accounts.groups || [];
      const ownAccount = state.accounts.find((account) => account.id === state.user?.id);
      if (ownAccount) {
        state.user.groupId = ownAccount.groupId;
        state.user.group = ownAccount.group;
      }
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
    renderWorkspaces();
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
      await refreshCurrentUser();
      const result = await api("/api/recharge");
      state.recharge = result;
      document.querySelector("#recharge-base-url").value = result.baseUrl || "";
      document.querySelector("#recharge-api-key").value = result.apiKey || "";
      document.querySelectorAll("[data-recharge-amount]").forEach((button) => {
        button.disabled = !result.paymentReady;
      });
      error.textContent = result.paymentReady ? "" : result.paymentError || t("recharge.paymentDisabled");
      renderRechargeHistory();
    } catch (loadError) {
      state.recharge = null;
      error.textContent = loadError.message;
      document.querySelectorAll("[data-recharge-amount]").forEach((button) => {
        button.disabled = true;
      });
      renderRechargeHistory();
    }
  }

  function rechargeImportStatus(status) {
    const key = `admin.importStatus.${status}`;
    const translated = t(key);
    return translated === key ? status : translated;
  }

  function renderRechargeImport() {
    const current = state.rechargeImport;
    const modal = document.querySelector("#recharge-import-modal");
    if (!current?.result) {
      modal.classList.add("hidden");
      return;
    }
    const result = current.result;
    const summary = result.summary || {};
    const parts = [
      `${t("admin.importTargetSheets")} ${Number(result.targetSheetCount || 0)}`,
      `${t("admin.importEligible")} ${Number(summary.eligible || 0)}`,
      `${t("admin.importApplied")} ${Number(summary.applied || 0)}`,
      `${t("admin.importDuplicate")} ${Number(summary.duplicate || 0)}`,
      `${t("admin.importSkipped")} ${Number(summary.invalid || 0) + Number(summary.unmatched || 0)}`,
      `${t("admin.importReview")} ${Number(summary.review_required || 0)}`,
    ];
    document.querySelector("#recharge-import-summary").textContent = parts.join(" · ");
    document.querySelector("#recharge-import-rows").innerHTML = (result.records || [])
      .map(
        (record) => `
          <tr>
            <td>${escapeMarkup(record.sheet)}:${Number(record.row || 0)}<br><small>${escapeMarkup(record.paymentRef || "")}</small></td>
            <td>${escapeMarkup(record.username || "-")}</td>
            <td>${escapeMarkup(record.paidAt || "-")}</td>
            <td>¥${escapeMarkup(record.amountCny || "0.00")} / ¥${escapeMarkup(record.creditCny || "0.00")}</td>
            <td><span class="recharge-import-status">${escapeMarkup(rechargeImportStatus(record.status))}</span>${record.reason ? `<br><small>${escapeMarkup(record.reason)}</small>` : ""}</td>
          </tr>
        `,
      )
      .join("");
    const confirm = document.querySelector("#recharge-import-confirm");
    confirm.disabled = current.applying || !current.file || Number(summary.eligible || 0) === 0;
    confirm.textContent = current.applying ? t("admin.importApplying") : t("admin.importConfirm");
    modal.classList.remove("hidden");
  }

  function closeRechargeImport() {
    state.rechargeImport = null;
    document.querySelector("#recharge-import-input").value = "";
    document.querySelector("#recharge-import-modal").classList.add("hidden");
  }

  async function previewRechargeImport(file) {
    const form = new FormData();
    form.append("file", file, file.name);
    try {
      const result = await api("/api/admin/recharge-imports/preview", { method: "POST", body: form });
      state.rechargeImport = { file, result, applying: false };
      document.querySelector("#recharge-import-error").textContent = "";
      renderRechargeImport();
    } catch (error) {
      state.rechargeImport = null;
      document.querySelector("#recharge-import-input").value = "";
      showToast(error.message);
    }
  }

  async function applyRechargeImport() {
    const current = state.rechargeImport;
    if (!current?.file || !current.result?.digest || current.applying) return;
    current.applying = true;
    document.querySelector("#recharge-import-error").textContent = "";
    renderRechargeImport();
    const form = new FormData();
    form.append("file", current.file, current.file.name);
    form.append("previewDigest", current.result.digest);
    try {
      current.result = await api("/api/admin/recharge-imports", { method: "POST", body: form });
      current.file = null;
      await loadAccountControlData();
      if (state.recharge) await loadRechargeData();
      showToast(t("toast.rechargeImported"));
    } catch (error) {
      document.querySelector("#recharge-import-error").textContent = error.message;
    } finally {
      current.applying = false;
      renderRechargeImport();
    }
  }

  function closeRechargeQrModal() {
    rechargeOrderGeneration += 1;
    window.clearTimeout(rechargeOrderPollTimer);
    rechargeOrderPollTimer = null;
    state.rechargeOrder = null;
    document.querySelector("#recharge-qr-modal").classList.add("hidden");
    document.querySelector("#recharge-qr-image").removeAttribute("src");
  }

  function renderRechargeOrderStatus(order) {
    renderPaymentNotice(document, order);
    const key = `recharge.order.${order?.status || "pending"}`;
    const translated = t(key);
    document.querySelector("#recharge-order-status").textContent = translated === key ? order?.status || "" : translated;
  }

  async function pollRechargeOrder(orderId) {
    if (state.rechargeOrder?.id !== orderId) return;
    try {
      const result = await api(`/api/recharge/orders/${encodeURIComponent(orderId)}`);
      if (state.rechargeOrder?.id !== orderId) return;
      state.rechargeOrder = result.order;
      renderRechargeOrderStatus(result.order);
      if (result.order.status === "applied") {
        await Promise.all([refreshCurrentUser(), loadRechargeData()]);
        return;
      }
      if (["expired", "closed", "failed", "review_required"].includes(result.order.status)) return;
    } catch (error) {
      document.querySelector("#recharge-order-status").textContent = error.message;
    }
    rechargeOrderPollTimer = window.setTimeout(() => pollRechargeOrder(orderId), 2000);
  }

  async function openRechargeQrModal(amount) {
    const product = state.recharge?.products?.find((item) => Number(item.amountCny) === Number(amount));
    if (!product) {
      showToast(t("recharge.notReady"));
      if (!state.recharge) loadRechargeData();
      return;
    }
    const image = document.querySelector("#recharge-qr-image");
    const missing = document.querySelector("#recharge-qr-missing");
    window.clearTimeout(rechargeOrderPollTimer);
    const generation = ++rechargeOrderGeneration;
    state.rechargeOrder = null;
    renderPaymentNotice(document, null);
    document.querySelector("#recharge-qr-amount").textContent = `¥${Number(product.amountCny).toFixed(0)}`;
    document.querySelector("#recharge-order-status").textContent = t("recharge.orderCreating");
    image.classList.add("hidden");
    missing.classList.add("hidden");
    image.onload = () => {
      if (generation !== rechargeOrderGeneration || ["paid", "crediting", "applied"].includes(state.rechargeOrder?.status)) return;
      image.classList.remove("hidden");
      missing.classList.add("hidden");
    };
    image.onerror = () => {
      if (generation !== rechargeOrderGeneration || ["paid", "crediting", "applied"].includes(state.rechargeOrder?.status)) return;
      image.classList.add("hidden");
      missing.classList.remove("hidden");
    };
    document.querySelector("#recharge-qr-modal").classList.remove("hidden");
    try {
      const result = await api("/api/recharge/orders", {
        method: "POST",
        body: JSON.stringify({ amountCny: product.amountCny }),
      });
      if (generation !== rechargeOrderGeneration) return;
      state.rechargeOrder = result.order;
      renderRechargeOrderStatus(result.order);
      image.src = authenticatedApiUrl(result.order.qrCodeUrl);
      pollRechargeOrder(result.order.id);
    } catch (error) {
      if (generation !== rechargeOrderGeneration) return;
      state.rechargeOrder = null;
      image.classList.add("hidden");
      missing.classList.remove("hidden");
      document.querySelector("#recharge-order-status").textContent = error.message;
    }
  }

  async function revokeInvite(userId) {
    try {
      await api(`/api/accounts/${encodeURIComponent(userId)}/revoke-invite`, { method: "POST" });
      closeAccountSettingsModal();
      await loadAccountControlData();
      showToast(t("toast.inviteRevoked"));
    } catch (error) {
      showToast(error.message);
    }
  }

  function openAccountSettingsModal(accountId) {
    if (!canManageAccounts(state.user)) return;
    const account = state.accounts.find((item) => item.id === accountId);
    if (!account) return;
    const form = document.querySelector("#account-settings-form");
    form.elements.accountId.value = account.id;
    form.elements.role.value = account.role || "user";
    form.elements.maxSessions.value = String(account.maxSessions ?? 1);
    form.elements.maxSessions.max = String(state.runtimeConfig.accountMaxSessionsLimit);
    form.elements.amountCny.value = "0.00";
    const groupSelect = form.elements.groupId;
    groupSelect.innerHTML = [
      `<option value="">${escapeMarkup(t("admin.ungroupedOption"))}</option>`,
      ...state.groups.map((group) => `<option value="${escapeMarkup(group.id)}">${escapeMarkup(group.name)}</option>`),
    ].join("");
    groupSelect.value = account.groupId || "";
    const balance = account.budget?.source === "custom"
      ? t("admin.customProvider")
      : account.budget?.remaining === null || account.budget?.remaining === undefined
        ? t("admin.balanceUnavailable")
        : `¥${account.budget.remaining}`;
    document.querySelector("#account-settings-name").textContent = `${account.username || account.id} · ${balance}`;
    document.querySelector("#account-settings-balance-field").classList.toggle("hidden", !isSystemAdmin(state.user) || !account.username || account.budget?.source === "custom");
    document.querySelector("#account-settings-error").textContent = "";
    const isCurrentUser = account.id === state.user?.id || account.username === state.user?.username;
    document.querySelector("#account-settings-delete").disabled = isCurrentUser;
    const invitePanel = document.querySelector("#account-settings-invite");
    invitePanel.classList.toggle("hidden", !isSystemAdmin(state.user) || !account.inviteToken);
    document.querySelector("#account-settings-invite-token").textContent = account.inviteToken || "";
    document.querySelector("#account-settings-copy-invite").dataset.copyInvite = account.inviteToken || "";
    document.querySelector("#account-settings-revoke-invite").dataset.revokeInvite = account.inviteToken ? account.id : "";
    document.querySelector("#account-settings-modal").classList.remove("hidden");
  }

  function closeAccountSettingsModal() {
    document.querySelector("#account-settings-modal").classList.add("hidden");
  }

  function openGroupSettingsModal(groupId) {
    if (!canManageAccounts(state.user)) return;
    const group = state.groups.find((item) => item.id === groupId);
    if (!group) return;
    const form = document.querySelector("#group-settings-form");
    form.elements.groupId.value = group.id;
    form.elements.name.value = group.name || "";
    form.elements.liveRunLimit.value = String(group.liveRunLimit ?? 1);
    form.elements.diskLimitMib.value = group.diskLimitBytes === null || group.diskLimitBytes === undefined
      ? ""
      : String(Math.round(Number(group.diskLimitBytes) / (1024 * 1024)));
    document.querySelector("#group-settings-summary").textContent = `${Number(group.userCount || 0)} ${t("admin.usersCount")}`;
    document.querySelector("#group-settings-error").textContent = "";
    const containsCurrentUser = state.accounts.some((account) =>
      account.groupId === group.id && (account.id === state.user?.id || account.username === state.user?.username));
    document.querySelector("#group-settings-delete").disabled = containsCurrentUser;
    document.querySelector("#group-settings-modal").classList.remove("hidden");
  }

  function closeGroupSettingsModal() {
    document.querySelector("#group-settings-modal").classList.add("hidden");
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

  async function deleteAdminAccount(userId) {
    if (!window.confirm(t("admin.confirmDeleteUser"))) return;
    try {
      await api(`/api/accounts/${encodeURIComponent(userId)}`, { method: "DELETE" });
      closeAccountSettingsModal();
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
      closeGroupSettingsModal();
      await loadAccountControlData();
      showToast(t("toast.groupDeleted"));
    } catch (error) {
      showToast(error.message);
    }
  }

  return { loadAccountControlData, refreshWorkerStatus, loadRuntimeConfig, setAuthMode, setGroupMode, renderGroupOptions, renderCreatedInvites, openBatchAccountModal, closeBatchAccountModal, copyText, loadRechargeData, rechargeImportStatus, renderRechargeImport, closeRechargeImport, previewRechargeImport, applyRechargeImport, closeRechargeQrModal, renderRechargeOrderStatus, pollRechargeOrder, openRechargeQrModal, revokeInvite, openAccountSettingsModal, closeAccountSettingsModal, openGroupSettingsModal, closeGroupSettingsModal, createAdminGroup, deleteAdminAccount, deleteAdminGroup };
}
