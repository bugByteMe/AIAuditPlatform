export const WORKSPACE_PATH_DRAG_TYPE = "application/x-ai-audit-workspace-path";

export function normalizeDroppedPath(value) {
  const path = String(value || "").replace(/^\/+/, "");
  const parts = path.split("/");
  if (!path || parts.some((part) => !part || part === "." || part === ".." || part.includes("\\") || part.includes("\0"))) {
    throw new Error("invalid_drop_path");
  }
  return parts.join("/");
}

async function readHandle(handle, prefix, items) {
  const path = normalizeDroppedPath([prefix, handle.name].filter(Boolean).join("/"));
  if (handle.kind === "file") {
    items.push({ file: await handle.getFile(), path });
    return;
  }
  if (handle.kind !== "directory") return;
  const children = [];
  for await (const child of handle.values()) children.push(child);
  children.sort((left, right) => left.name.localeCompare(right.name));
  for (const child of children) await readHandle(child, path, items);
}

function readEntryBatch(reader) {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function readEntry(entry, prefix, items) {
  const path = normalizeDroppedPath([prefix, entry.name].filter(Boolean).join("/"));
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    items.push({ file, path });
    return;
  }
  if (!entry.isDirectory) return;
  const reader = entry.createReader();
  const children = [];
  while (true) {
    const batch = await readEntryBatch(reader);
    if (!batch.length) break;
    children.push(...batch);
  }
  children.sort((left, right) => left.name.localeCompare(right.name));
  for (const child of children) await readEntry(child, path, items);
}

function fallbackFileItem(file) {
  return { file, path: normalizeDroppedPath(file.webkitRelativePath || file.name) };
}

export function hasExternalFiles(dataTransfer) {
  return [...(dataTransfer?.types || [])].includes("Files");
}

export async function collectDroppedUpload(dataTransfer) {
  const transferItems = [...(dataTransfer?.items || [])].filter((item) => item.kind === "file");
  const descriptors = transferItems.map((item) => ({
    handlePromise: typeof item.getAsFileSystemHandle === "function"
      ? Promise.resolve(item.getAsFileSystemHandle()).catch(() => null)
      : null,
    entry: typeof item.webkitGetAsEntry === "function" ? item.webkitGetAsEntry() : null,
    file: item.getAsFile?.() || null,
  }));
  const items = [];
  const topLevelPaths = [];
  for (const descriptor of descriptors) {
    const start = items.length;
    const handle = descriptor.handlePromise ? await descriptor.handlePromise : null;
    const fallbackPath = descriptor.file?.webkitRelativePath || descriptor.file?.name || "";
    const name = handle?.name || descriptor.entry?.name || fallbackPath.split("/")[0];
    if (handle) await readHandle(handle, "", items);
    else if (descriptor.entry) await readEntry(descriptor.entry, "", items);
    else if (descriptor.file) items.push(fallbackFileItem(descriptor.file));
    if (items.length > start && name) {
      const root = normalizeDroppedPath(name);
      if (!topLevelPaths.includes(root)) topLevelPaths.push(root);
    }
  }
  if (!descriptors.length) {
    for (const file of [...(dataTransfer?.files || [])]) {
      items.push(fallbackFileItem(file));
      const root = normalizeDroppedPath(file.webkitRelativePath || file.name).split("/")[0];
      if (!topLevelPaths.includes(root)) topLevelPaths.push(root);
    }
  }
  items.sort((left, right) => left.path.localeCompare(right.path));
  const seen = new Set();
  for (const item of items) {
    if (seen.has(item.path)) throw new Error("duplicate_drop_path");
    seen.add(item.path);
  }
  return { items, topLevelPaths };
}

export function joinWorkspacePath(parent, child) {
  return [String(parent || "").replace(/\/+$/, ""), normalizeDroppedPath(child)].filter(Boolean).join("/");
}

export function insertPathsAtSelection(textarea, paths) {
  const text = [...paths].filter(Boolean).join("\n");
  if (!textarea || !text) return false;
  const start = Number.isInteger(textarea.selectionStart) ? textarea.selectionStart : textarea.value.length;
  const end = Number.isInteger(textarea.selectionEnd) ? textarea.selectionEnd : start;
  const before = textarea.value.slice(0, start);
  const after = textarea.value.slice(end);
  const prefix = before && !before.endsWith("\n") ? "\n" : "";
  const suffix = after && !after.startsWith("\n") ? "\n" : "";
  const insertion = `${prefix}${text}${suffix}`;
  textarea.setRangeText(insertion, start, end, "end");
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  textarea.focus();
  return true;
}
