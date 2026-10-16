// Both workspace trees use the same initial expansion threshold.
export function defaultCollapsedFolders(files) {
  return new Set((files || [])
    .filter((file) => file.type === "folder" && file.hasChildren && (!file.childrenLoaded || Number(file.childCount || 0) > 5))
    .map((file) => file.path));
}
