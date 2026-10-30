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

export function workspaceGroupsMatch(left, right) {
  const leftId = String(left?.groupId || "");
  const rightId = String(right?.groupId || "");
  if (leftId || rightId) return Boolean(leftId && rightId && leftId === rightId);
  const leftName = normalized(left?.group);
  const rightName = normalized(right?.group);
  return Boolean(leftName && rightName && leftName === rightName);
}

export function workspaceDescendantIds(items, workspaceId) {
  const descendants = new Set();
  let changed = true;
  while (changed) {
    changed = false;
    items.forEach(({ workspace }) => {
      const parentId = String(workspace.parentWorkspaceId || "");
      if (!descendants.has(workspace.id) && (parentId === workspaceId || descendants.has(parentId))) {
        descendants.add(workspace.id);
        changed = true;
      }
    });
  }
  descendants.delete(workspaceId);
  return descendants;
}

export function workspaceAncestorIds(items, workspaceIds) {
  const byId = new Map(items.map((item) => [item.workspace.id, item.workspace]));
  const ancestors = new Set();
  workspaceIds.forEach((workspaceId) => {
    const visited = new Set([workspaceId]);
    let parentId = String(byId.get(workspaceId)?.parentWorkspaceId || "");
    while (parentId && byId.has(parentId) && !visited.has(parentId)) {
      ancestors.add(parentId);
      visited.add(parentId);
      parentId = String(byId.get(parentId)?.parentWorkspaceId || "");
    }
  });
  return ancestors;
}

export function buildWorkspaceHierarchy(items, includedIds = null) {
  const nodes = new Map(items.map((item) => [item.workspace.id, { ...item, children: [], depth: 0 }]));
  const validParent = new Map();
  items.forEach(({ workspace }) => {
    const parentId = String(workspace.parentWorkspaceId || "");
    if (!parentId || parentId === workspace.id || !nodes.has(parentId)) return;
    const visited = new Set([workspace.id]);
    let cursor = parentId;
    while (cursor && nodes.has(cursor) && !visited.has(cursor)) {
      visited.add(cursor);
      cursor = String(nodes.get(cursor).workspace.parentWorkspaceId || "");
    }
    if (!cursor || !nodes.has(cursor)) validParent.set(workspace.id, parentId);
  });
  const roots = [];
  items.forEach(({ workspace }) => {
    const node = nodes.get(workspace.id);
    const parent = nodes.get(validParent.get(workspace.id));
    if (parent) parent.children.push(node);
    else roots.push(node);
  });
  const assignDepth = (node, depth) => {
    node.depth = depth;
    node.children.forEach((child) => assignDepth(child, depth + 1));
  };
  roots.forEach((root) => assignDepth(root, 0));
  if (!includedIds) return roots;
  const prune = (node) => {
    const children = node.children.map(prune).filter(Boolean);
    if (!includedIds.has(node.workspace.id) && !children.length) return null;
    return { ...node, children };
  };
  return roots.map(prune).filter(Boolean);
}

export function flattenWorkspaceHierarchy(nodes) {
  const result = [];
  const visit = (node) => {
    result.push(node);
    node.children.forEach(visit);
  };
  nodes.forEach(visit);
  return result;
}

export function workspaceNodeInitiallyExpanded(depth) {
  return depth === 0;
}
