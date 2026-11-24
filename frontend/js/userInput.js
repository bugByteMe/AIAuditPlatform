import { api, authenticatedApiUrl } from "./api.js";
import { markdownToHtml } from "./chatMarkdown.js";

export const inputDrafts = new Map();
const requests = new Map();
const loaded = new Set();
const busy = new Set();
const errors = new Map();
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const endpoint = (request) => `/api/workspaces/${encodeURIComponent(request.workspaceId)}/chat/input-requests/${encodeURIComponent(request.id)}?sessionId=${encodeURIComponent(request.sessionId)}`;

export function mergeInputRequest(request) {
  if (!request?.id) return;
  const existing = requests.get(request.id);
  // A delayed history/load response must never reopen a resolved card.
  if (existing && existing.status !== "pending" && request.status === "pending") return;
  requests.set(request.id, request);
}

export function syncInputRequests(session, workspaceId, rerender) {
  for (const event of session.events || []) mergeInputRequest(event[7]?.inputRequest);
  const key = `${workspaceId}/${session.id}`;
  if (!loaded.has(key)) {
    loaded.add(key);
    api(`/api/workspaces/${encodeURIComponent(workspaceId)}/chat/input-requests?sessionId=${encodeURIComponent(session.id)}`)
      .then((result) => { result.requests.forEach(mergeInputRequest); rerender(); })
      .catch(() => { loaded.delete(key); });
  }
  return [...requests.values()].filter((request) => request.sessionId === session.id);
}

export function inputRequestSignature(sessionId) {
  return JSON.stringify([...requests.values()].filter((request) => request.sessionId === sessionId));
}

export function renderInputRequest(original, user, lang, active) {
  const request = requests.get(original.id) || original;
  const zh = lang === "zh";
  const question = request.question;
  const enabled = active && request.status === "pending" && user?.username === request.initiator && !busy.has(request.id);
  const disabled = enabled ? "" : " disabled";
  const draft = inputDrafts.get(request.id);
  const value = request.result?.value ?? draft;
  let controls = "";
  if (question.type === "text") {
    controls = `<textarea aria-labelledby="input-title-${escape(request.id)}" data-input-answer maxlength="16384"${disabled}>${escape(value || "")}</textarea>`;
  } else {
    const options = question.type === "yes_no"
      ? [{ id: "true", label: zh ? "是" : "Yes" }, { id: "false", label: zh ? "否" : "No" }]
      : question.options;
    controls = options.map((option) => {
      const checked = question.type === "yes_no" ? String(value) === option.id : Array.isArray(value) && value.includes(option.id);
      return `<label><input data-input-answer type="${question.multiple ? "checkbox" : "radio"}" name="input-${escape(request.id)}" value="${escape(option.id)}"${checked ? " checked" : ""}${disabled}> ${escape(option.label)}</label>`;
    }).join("");
  }
  const images = (request.images || []).map((image) => `<img loading="lazy" src="${escape(authenticatedApiUrl(`${endpoint(request)}&image=${image.index}`))}" alt="${escape(image.name)}">`).join("");
  const label = { pending: zh ? "等待你的输入" : "Waiting for your input", answered: zh ? "已回答" : "Answered", skipped: zh ? "已跳过" : "Skipped", cancelled: zh ? "已取消" : "Cancelled" }[request.status];
  const buttons = request.status === "pending" && active && user?.username === request.initiator
    ? `<button type="submit" class="btn primary"${disabled}>${zh ? "提交" : "Submit"}</button><button type="button" class="btn ghost" data-input-skip${disabled}>${zh ? "跳过" : "Skip"}</button>` : "";
  return `<form class="user-input-card" data-user-input="${escape(request.id)}"><div role="status">${escape(label)}</div><h4 id="input-title-${escape(request.id)}">${escape(question.title)}</h4><div class="event-markdown">${markdownToHtml(question.text || "")}</div><div class="user-input-images">${images}</div><fieldset aria-labelledby="input-title-${escape(request.id)}"${disabled}>${controls}</fieldset><div class="user-input-actions">${buttons}</div><p class="user-input-error" role="alert">${escape(errors.get(request.id) || "")}</p></form>`;
}

export function bindUserInput(stream, rerender) {
  stream.querySelectorAll("[data-user-input]").forEach((form) => {
    const request = requests.get(form.dataset.userInput);
    if (!request) return;
    const saveDraft = () => {
      let value;
      if (request.question.type === "text") value = form.querySelector("textarea").value;
      else {
        const selected = [...form.querySelectorAll("input:checked")].map((node) => node.value);
        value = request.question.type === "yes_no" ? (selected.length ? selected[0] === "true" : undefined) : selected;
      }
      inputDrafts.set(request.id, value);
      return value;
    };
    form.addEventListener("input", saveDraft);
    const submit = async (skip) => {
      if (busy.has(request.id)) return;
      const value = saveDraft();
      if (!skip && (value === undefined || (Array.isArray(value) && !value.length) || (typeof value === "string" && (!value.trim() || new TextEncoder().encode(value).length > 16384)))) {
        errors.set(request.id, "Please enter a valid answer / 请填写有效答案");
        form.querySelector("[role=alert]").textContent = errors.get(request.id);
        return;
      }
      busy.add(request.id);
      errors.delete(request.id);
      form.querySelectorAll("button,input,textarea").forEach((node) => { node.disabled = true; });
      try {
        const result = await api(endpoint(request), { method: "POST", body: JSON.stringify(skip ? { status: "skipped" } : { status: "answered", value }) });
        mergeInputRequest(result.request);
        inputDrafts.delete(request.id);
      } catch (error) {
        errors.set(request.id, error.message || "Submission failed. Retry / 提交失败，请重试");
        // Refresh canonical state after a conflict or an uncertain network result.
        try {
          const result = await api(`/api/workspaces/${encodeURIComponent(request.workspaceId)}/chat/input-requests?sessionId=${encodeURIComponent(request.sessionId)}`);
          result.requests.forEach(mergeInputRequest);
        } catch { /* Keep the draft and allow retry. */ }
      } finally {
        busy.delete(request.id);
        rerender(true);
      }
    };
    form.addEventListener("submit", (event) => { event.preventDefault(); submit(false); });
    form.querySelector("[data-input-skip]")?.addEventListener("click", () => submit(true));
  });
}

export function captureInputFocus(stream) {
  const node = document.activeElement;
  const form = node?.closest?.("[data-user-input]");
  if (!form || !stream.contains(form)) return null;
  return { id: form.dataset.userInput, value: node.value, tag: node.tagName,
    start: node.selectionStart, end: node.selectionEnd };
}

export function restoreInputFocus(stream, focus) {
  if (!focus) return;
  const form = [...stream.querySelectorAll("[data-user-input]")].find((item) => item.dataset.userInput === focus.id);
  const node = [...(form?.querySelectorAll("input,textarea") || [])].find((item) => item.tagName === focus.tag && (item.tagName === "TEXTAREA" || item.value === focus.value));
  if (!node || node.disabled) return;
  node.focus({ preventScroll: true });
  if (node.tagName === "TEXTAREA" && focus.start != null) node.setSelectionRange(focus.start, focus.end);
}
