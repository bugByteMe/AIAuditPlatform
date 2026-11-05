import { destroyFileTree, renderDestinationFileTree } from "./fileTreeAdapter.js";

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
    if (!picker) {
      destroyFileTree(list);
      list.replaceChildren();
      return;
    }
    renderDestinationFileTree(list, {
      workspaceId: picker.workspaceId,
      items: picker.items,
      selectedPath: picker.selected,
      onSelect(path) {
        if (!picker) return;
        picker.selected = path;
        document.querySelector("#workspace-destination-selected").textContent = path || t("files.workspaceRoot");
      },
      async loadFolder(path) {
        const current = picker;
        const query = new URLSearchParams({ path, depth: "1" });
        const result = await api(`/api/workspaces/${encodeURIComponent(current.workspaceId)}/files?${query}`);
        if (picker !== current) return [];
        const folders = (result.files || []).filter((item) => item.type === "folder");
        const files = new Map(current.items.map((item) => [item.path, item]));
        folders.forEach((item) => files.set(item.path, item));
        current.items = [...files.values()];
        return folders;
      },
    });
    document.querySelector("#workspace-destination-selected").textContent = picker.selected || t("files.workspaceRoot");
  }

  async function openDestinationPicker(kind) {
    const workspace = safeCurrentWorkspace();
    if (!workspace || !["files", "folder"].includes(kind)) return;
    nativeSelection = null;
    const current = { workspaceId: workspace.id, kind, selected: "", items: [], loaded: false };
    picker = current;
    document.querySelector("#workspace-destination-modal").classList.remove("hidden");
    renderDestinationPicker();
    try {
      const query = new URLSearchParams({ depth: "1" });
      const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/files?${query}`);
      if (picker !== current) return;
      current.items = (result.files || []).filter((item) => item.type === "folder");
      current.loaded = true;
      renderDestinationPicker();
    } catch (error) {
      if (picker !== current) return;
      closeDestinationPicker();
      showToast(error.message);
    }
  }

  function closeDestinationPicker() {
    destroyFileTree(document.querySelector("#workspace-destination-list"));
    picker = null;
    document.querySelector("#workspace-destination-modal").classList.add("hidden");
  }

  function confirmDestinationPicker() {
    if (!picker || !picker.loaded) return;
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
    confirmDestinationPicker, acceptNativeUpload,
  };
}
