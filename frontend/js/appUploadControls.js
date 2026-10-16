// Workspace upload cancellation and the lazy destination-folder picker.
export function createUploadControls(context) {
  const { api, state, t, showToast, loadWorkspaces, safeCurrentWorkspace, uploadFilesToCurrentWorkspace } = context;
  let picker = null;
  let nativeSelection = null;

  async function cancelWorkspaceUpload(workspaceId) {
    const workspace = state.workspaces.find((item) => item.id === workspaceId);
    const upload = workspace?.activeUpload;
    if (!upload || upload.status !== "uploading") return;
    if (!window.confirm(t("workspace.cancelUploadConfirm").replace("{name}", workspace.name))) return;
    try {
      await api(`/api/uploads/${encodeURIComponent(upload.id)}`, { method: "DELETE" });
      try {
        const saved = JSON.parse(window.localStorage.getItem("aiAuditPendingUpload") || "null");
        if (saved?.uploadId === upload.id) window.localStorage.removeItem("aiAuditPendingUpload");
      } catch { /* A malformed browser resume record does not affect server cancellation. */ }
      showToast(t("toast.uploadCancelled"));
    } catch (error) {
      showToast(error.code === "upload_commit_started" ? t("workspace.uploadProcessing") : error.message);
    } finally {
      await loadWorkspaces();
    }
  }

  function renderDestinationPicker() {
    const list = document.querySelector("#workspace-destination-list");
    list.replaceChildren();
    if (!picker) return;
    const appendRow = (path, label, level, hasChildren) => {
      const row = document.createElement("div");
      row.className = "destination-folder-row";
      row.style.setProperty("--folder-level", level);
      if (path) {
        const expand = document.createElement("button");
        expand.type = "button";
        expand.className = "destination-expand";
        expand.dataset.destinationExpand = path;
        expand.textContent = hasChildren ? (picker.expanded.has(path) ? "▾" : "▸") : "·";
        expand.disabled = !hasChildren || picker.loading.has(path);
        expand.setAttribute("aria-label", t("files.expand"));
        row.append(expand);
      }
      const select = document.createElement("button");
      select.type = "button";
      select.className = "destination-select";
      select.dataset.destinationPath = path;
      select.setAttribute("aria-pressed", String(picker.selected === path));
      select.textContent = label;
      row.append(select);
      list.append(row);
    };
    appendRow("", t("files.workspaceRoot"), 0, true);
    const appendChildren = (parent, level) => {
      for (const folder of picker.children.get(parent) || []) {
        appendRow(folder.path, folder.name, level, folder.hasChildren);
        if (picker.expanded.has(folder.path)) appendChildren(folder.path, level + 1);
      }
    };
    appendChildren("", 1);
    document.querySelector("#workspace-destination-selected").textContent = picker.selected || t("files.workspaceRoot");
  }

  async function openDestinationPicker(kind) {
    const workspace = safeCurrentWorkspace();
    if (!workspace || !["files", "folder"].includes(kind)) return;
    nativeSelection = null;
    const current = { workspaceId: workspace.id, kind, selected: "", children: new Map(), expanded: new Set(), loading: new Set() };
    picker = current;
    document.querySelector("#workspace-destination-modal").classList.remove("hidden");
    renderDestinationPicker();
    try {
      const query = new URLSearchParams({ depth: "1" });
      const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/files?${query}`);
      if (picker !== current) return;
      current.children.set("", (result.files || []).filter((item) => item.type === "folder"));
      renderDestinationPicker();
    } catch (error) {
      if (picker !== current) return;
      closeDestinationPicker();
      showToast(error.message);
    }
  }

  function closeDestinationPicker() {
    picker = null;
    document.querySelector("#workspace-destination-modal").classList.add("hidden");
  }

  function selectDestinationFolder(path) {
    if (!picker) return;
    picker.selected = path;
    renderDestinationPicker();
  }

  async function expandDestinationFolder(path) {
    const current = picker;
    if (!current || current.loading.has(path)) return;
    if (current.expanded.has(path)) {
      current.expanded.delete(path);
      renderDestinationPicker();
      return;
    }
    if (!current.children.has(path)) {
      current.loading.add(path);
      renderDestinationPicker();
      try {
        const query = new URLSearchParams({ path, depth: "1" });
        const result = await api(`/api/workspaces/${encodeURIComponent(current.workspaceId)}/files?${query}`);
        if (picker !== current) return;
        current.children.set(path, (result.files || []).filter((item) => item.type === "folder"));
      } catch (error) {
        if (picker === current) showToast(error.message);
        return;
      } finally {
        current.loading.delete(path);
        if (picker === current) renderDestinationPicker();
      }
    }
    current.expanded.add(path);
    renderDestinationPicker();
  }

  function confirmDestinationPicker() {
    if (!picker || !picker.children.has("")) return;
    nativeSelection = { workspaceId: picker.workspaceId, kind: picker.kind, destinationPath: picker.selected };
    const input = document.querySelector(nativeSelection.kind === "folder" ? "#workspace-add-folder-input" : "#workspace-add-files-input");
    closeDestinationPicker();
    input.click();
  }

  function acceptNativeUpload(files, kind) {
    const selection = nativeSelection;
    nativeSelection = null;
    if (!selection || selection.kind !== kind || !files.length) return;
    uploadFilesToCurrentWorkspace(files, selection.destinationPath, selection.workspaceId);
  }

  return {
    cancelWorkspaceUpload, openDestinationPicker, closeDestinationPicker,
    selectDestinationFolder, expandDestinationFolder, confirmDestinationPicker, acceptNativeUpload,
  };
}
