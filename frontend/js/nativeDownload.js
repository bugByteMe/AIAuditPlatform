const DOWNLOAD_COOKIE_PREFIX = "ai_audit_download_";

function cookiePresent(cookieText, name) {
  return String(cookieText || "").split(";").some((item) => item.trim() === `${name}=1`);
}

export function startMonitoredDownload(url, options = {}) {
  const documentObject = options.documentObject || globalThis.document;
  const locationObject = options.locationObject || globalThis.window.location;
  const cryptoObject = options.cryptoObject || globalThis.crypto;
  const setIntervalFunction = options.setIntervalFunction || globalThis.setInterval;
  const clearIntervalFunction = options.clearIntervalFunction || globalThis.clearInterval;
  const setTimeoutFunction = options.setTimeoutFunction || globalThis.setTimeout;
  const now = options.now || Date.now;
  const timeoutMs = Number(options.timeoutMs || 120_000);
  const token = cryptoObject.randomUUID().replaceAll("-", "");
  const cookieName = `${DOWNLOAD_COOKIE_PREFIX}${token}`;
  const downloadUrl = new URL(url, locationObject.href);
  downloadUrl.searchParams.set("downloadToken", token);
  const sameOrigin = downloadUrl.origin === new URL(locationObject.href).origin;
  const startedAt = now();
  let timer = null;

  options.onPreparing?.();
  const anchor = documentObject.createElement("a");
  anchor.href = downloadUrl.toString();
  anchor.rel = "noopener";
  anchor.style.display = "none";
  documentObject.body.append(anchor);
  anchor.click();
  setTimeoutFunction(() => anchor.remove(), 1_000);

  if (!sameOrigin) {
    setTimeoutFunction(() => options.onStarted?.(), 1_000);
    return { token, cancel() {} };
  }

  const finish = (callback) => {
    if (timer !== null) clearIntervalFunction(timer);
    timer = null;
    callback?.();
  };
  const poll = () => {
    if (cookiePresent(documentObject.cookie, cookieName)) {
      documentObject.cookie = `${cookieName}=; Path=/; Max-Age=0; SameSite=Lax`;
      finish(options.onStarted);
    } else if (now() - startedAt >= timeoutMs) {
      finish(options.onTimeout);
    }
  };
  timer = setIntervalFunction(poll, 100);
  poll();
  return { token, cancel: () => finish() };
}

export { cookiePresent };
