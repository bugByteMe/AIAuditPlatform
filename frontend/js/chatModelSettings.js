export const DEFAULT_CHAT_MODEL = "gpt-5.6-sol";
export const DEFAULT_CHAT_REASONING = "high";

export function applyChatModelSettings(root, session, appliedSessionId = "") {
  if (!session || appliedSessionId === session.id) return session?.id || "";
  const modelSelect = root.querySelector('#composer [name="model"]');
  const reasoningSelect = root.querySelector('#composer [name="reasoning"]');
  const model = session.model || DEFAULT_CHAT_MODEL;
  const reasoning = session.reasoning || DEFAULT_CHAT_REASONING;
  if (modelSelect && [...modelSelect.options].some((option) => option.value === model)) modelSelect.value = model;
  if (reasoningSelect && [...reasoningSelect.options].some((option) => option.value === reasoning)) reasoningSelect.value = reasoning;
  return session.id;
}
