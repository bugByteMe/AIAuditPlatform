export function localizedCapabilityText(value, lang = "en") {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "";
  return String(value[lang] || value.en || value.zh || "");
}

export function filterCapabilities(capabilities, query, lang = "en") {
  const normalized = String(query || "").trim().toLocaleLowerCase();
  if (!normalized) return [...(capabilities || [])];
  return (capabilities || []).filter((capability) => [
    capability.id,
    localizedCapabilityText(capability.name, lang),
    localizedCapabilityText(capability.description, lang),
  ].some((value) => String(value || "").toLocaleLowerCase().includes(normalized)));
}

export function reconcileCapabilityIds(selectedIds, capabilities) {
  const available = new Set((capabilities || []).map((item) => item.id));
  return [...(selectedIds || [])].filter((id) => available.has(id));
}

export function defaultCapabilityIds(capabilities) {
  return (capabilities || []).filter((item) => item.default).map((item) => item.id);
}
