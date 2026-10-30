export function reconcileCollaboratingWorkspaceIds(selectedIds, candidates) {
  const eligible = new Set((candidates || []).map((item) => item.id));
  return [...new Set(selectedIds || [])].filter((id) => eligible.has(id));
}

export function filterCollaboratingWorkspaces(candidates, query) {
  const normalized = String(query || "").trim().toLocaleLowerCase();
  if (!normalized) return candidates || [];
  return (candidates || []).filter((item) => (
    `${item.name || ""} ${item.id || ""}`.toLocaleLowerCase().includes(normalized)
  ));
}
