// Resolve authentication before revealing either login or the application shell.
export async function resolveStartup({ loadConfig, loadSession, authenticated, login }) {
  await loadConfig();
  let session;
  try {
    session = await loadSession();
  } catch (error) {
    if (error.status !== 401) throw error;
    login();
    return false;
  }
  authenticated(session.user);
  return true;
}

export function setStartupScreen(phase, lang = "zh") {
  const zh = lang === "zh";
  document.documentElement.dataset.startup = phase;
  const screen = document.querySelector("#startup-screen");
  screen.hidden = phase === "ready";
  screen.setAttribute("aria-busy", String(phase === "loading"));
  document.querySelector("#startup-message").textContent = phase === "error"
    ? (zh ? "暂时无法连接服务，请重试。" : "Unable to connect. Please retry.")
    : (zh ? "正在加载…" : "Loading…");
  document.querySelector("#startup-retry").hidden = phase !== "error";
  document.querySelector("#startup-retry").textContent = zh ? "重试" : "Retry";
}

export function dataPanelMarkup(status, lang = "zh") {
  const error = status === "error";
  const text = lang === "zh"
    ? (error ? "加载失败，请刷新重试。" : "正在加载…")
    : (error ? "Could not load data. Refresh to retry." : "Loading…");
  return `<div class="data-loading" role="${error ? "alert" : "status"}">${error ? "" : '<span class="loading-dot" aria-hidden="true"></span>'}<span>${text}</span></div>`;
}
