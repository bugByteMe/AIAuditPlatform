// Frontend controller: uploads responsibilities.
export function createUploadActions(context) {
  const api = (...args) => context.api(...args);
  const uploadChunkApi = (...args) => context.uploadChunkApi(...args);
  const t = (...args) => context.t(...args);
  const state = context.state;
  const setOperationProgress = (...args) => context.setOperationProgress(...args);

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
      if (!["uploading", "processing", "committing"].includes(upload.status)) {
        window.localStorage.removeItem(PENDING_UPLOAD_KEY);
        throw new Error(upload.error || "Upload processing failed");
      }
      if (signal?.aborted && upload.status === "uploading") throw new DOMException("Upload aborted", "AbortError");
      setOperationProgress(t(upload.phase === "committing" ? "progress.committingUpload" : "progress.processingUpload"), 90, true);
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      upload = (await api(`/api/uploads/${encodeURIComponent(upload.id)}`)).upload;
    }
    window.localStorage.removeItem(PENDING_UPLOAD_KEY);
    setOperationProgress(t("progress.committingUpload"), 100);
    return upload.workspace;
  }

  return { uploadFingerprint, runResumableUpload };
}
