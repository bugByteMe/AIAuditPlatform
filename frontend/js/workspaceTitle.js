import { t } from "./i18n.js";

export function workspaceTitle(user, groups = []) {
  if (["system_admin", "platform_admin"].includes(user?.role)) return t("workspace.title");
  if (!user?.groupId) return t("workspace.title");
  const group = groups.find((item) => item.id === user.groupId);
  const name = String(group ? group.name || "" : user.group || "").trim();
  return name ? t("workspace.groupTitle").replace("{name}", name) : t("workspace.title");
}

export function renderWorkspaceTitle(root, user, groups) {
  const title = root.querySelector("#workspace-overview-title");
  if (title) title.textContent = workspaceTitle(user, groups);
}
