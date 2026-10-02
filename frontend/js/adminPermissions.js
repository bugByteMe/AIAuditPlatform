export function canManageAccounts(user) {
  return ["system_admin", "platform_admin"].includes(user?.role);
}

export function isSystemAdmin(user) {
  return user?.role === "system_admin";
}

export function accountSettingsPayload(user, form) {
  const payload = { groupId: String(form.get("groupId") || "") };
  if (isSystemAdmin(user)) {
    payload.maxSessions = Number(form.get("maxSessions"));
    payload.role = String(form.get("role") || "user");
  }
  return payload;
}

export function groupSettingsPayload(user, form) {
  const payload = { name: String(form.get("name") || "").trim(), liveRunLimit: Number(form.get("liveRunLimit")) };
  if (isSystemAdmin(user)) {
    const raw = String(form.get("diskLimitMib") || "").trim();
    payload.diskLimitBytes = raw === "" ? null : Math.round(Number(raw) * 1024 * 1024);
  }
  return payload;
}

export function applyAdminPermissions(root, user) {
  root.querySelectorAll("[data-system-admin-only]").forEach((node) => {
    const allowed = isSystemAdmin(user);
    node.classList.toggle("hidden", !allowed);
    const controls = node.matches("input, select, button") ? [node] : node.querySelectorAll("input, select, button");
    controls.forEach((control) => { control.disabled = !allowed; });
  });
}
