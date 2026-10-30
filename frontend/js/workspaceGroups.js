function normalized(value) {
  return String(value || "").trim().toLocaleLowerCase();
}

export function resolveWorkspaceGroup(workspace, user, groups = [], accounts = []) {
  const owner = accounts.find((account) => account.username === workspace.owner);
  const groupId = String(owner?.groupId || "");
  const group = groups.find((item) => item.id === groupId)
    || groups.find((item) => normalized(item.name) === normalized(workspace.group));
  const resolvedId = groupId || String(group?.id || "");
  const name = String(group?.name || workspace.group || "").trim();
  const key = resolvedId || (name ? `name:${normalized(name)}` : "ungrouped");
  const own = Boolean(
    (resolvedId && user?.groupId && resolvedId === user.groupId)
    || (!resolvedId && name && normalized(name) === normalized(user?.group)),
  );
  return { key, id: resolvedId, name, own };
}

export function groupWorkspaces(workspaces, user, groups = [], accounts = []) {
  const sections = new Map();
  workspaces.forEach(({ workspace, index }) => {
    const group = resolveWorkspaceGroup(workspace, user, groups, accounts);
    if (!sections.has(group.key)) sections.set(group.key, { ...group, workspaces: [] });
    sections.get(group.key).workspaces.push({ workspace, index });
  });
  return [...sections.values()].sort((left, right) => {
    if (left.own !== right.own) return left.own ? -1 : 1;
    return (left.name || "\uffff").localeCompare(right.name || "\uffff");
  });
}

export function defaultExpandedWorkspaceGroups(sections) {
  const own = sections.find((section) => section.own);
  return new Set(own ? [own.key] : []);
}
