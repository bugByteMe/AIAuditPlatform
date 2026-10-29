import { t } from "./i18n.js";

const API_BASE_KEY = "aiAuditApiBase";
const SESSION_TOKEN_KEY = "aiAuditSessionToken";
const DEFAULT_BACKEND_BASE = "http://127.0.0.1:8765";
const SESSION_EXPIRED_EVENT = "ai-audit:session-expired";
let sessionExpirationNotified = false;

function unique(values) {
  return [...new Set(values.filter((value) => value !== null && value !== undefined))];
}

function currentApiBase() {
  return normalizeApiBase(window.localStorage.getItem(API_BASE_KEY) || "");
}

function normalizeApiBase(base) {
  return String(base || "").replace(/\/+$/, "");
}

function backendCandidates() {
  const currentBase = `${window.location.protocol}//${window.location.host}`;
  const isLocalPage = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
  const candidates = [currentApiBase(), ""];
  if (isLocalPage && currentBase !== DEFAULT_BACKEND_BASE) candidates.push(DEFAULT_BACKEND_BASE);
  return unique(candidates);
}

function buildUrl(path, base = currentApiBase()) {
  if (/^https?:\/\//i.test(path)) return path;
  const normalizedBase = normalizeApiBase(base);
  if (!normalizedBase) return path;
  return `${normalizedBase}/${path.replace(/^\/+/, "")}`;
}

function rememberApiBase(base) {
  const normalizedBase = normalizeApiBase(base);
  if (normalizedBase) window.localStorage.setItem(API_BASE_KEY, normalizedBase);
}

function rememberSessionToken(path, payload) {
  if (path === "/api/runtime-config" && payload.authMode === "oidc") {
    // OIDC uses an HttpOnly cookie, not a stale legacy bearer token.
    window.localStorage.removeItem(SESSION_TOKEN_KEY);
  }
  if (["/api/login", "/api/register"].includes(path) && payload.sessionToken) {
    window.localStorage.setItem(SESSION_TOKEN_KEY, payload.sessionToken);
    sessionExpirationNotified = false;
  }
  if (path === "/api/logout") {
    window.localStorage.removeItem(SESSION_TOKEN_KEY);
  }
}

function shouldNotifySessionExpiry(path, options) {
  if (options.suppressSessionExpiry) return false;
  if (options.notifySessionExpiry) return true;
  let pathname = String(path || "");
  try {
    pathname = new URL(pathname, window.location.href).pathname;
  } catch {
    // Preserve the raw relative path when URL parsing is unavailable.
  }
  return !["/api/login", "/api/register"].includes(pathname);
}

function notifySessionExpired(path, options = {}) {
  if (!shouldNotifySessionExpiry(path, options) || sessionExpirationNotified) return;
  sessionExpirationNotified = true;
  window.localStorage.removeItem(SESSION_TOKEN_KEY);
  if (typeof window.dispatchEvent === "function" && typeof Event === "function") {
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  }
}

function responseErrorMessage(payload) {
  if (payload.error === "server_busy") {
    const workload = ["file", "upload", "external"].includes(payload.workload) ? payload.workload : "generic";
    return t(`errors.serverBusy.${workload}`);
  }
  return payload.message || payload.error || "request_failed";
}

async function fetchJson(path, options, base) {
  const { suppressSessionExpiry, notifySessionExpiry, ...fetchOptions } = options;
  const requestPolicy = { suppressSessionExpiry, notifySessionExpiry };
  const headers = { ...(fetchOptions.headers || {}) };
  if (!(fetchOptions.body instanceof FormData)) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
  }
  const token = window.localStorage.getItem(SESSION_TOKEN_KEY);
  if (token) headers.Authorization = headers.Authorization || `Bearer ${token}`;
  let response;
  try {
    response = await fetch(buildUrl(path, base), {
      credentials: "include",
      headers,
      ...fetchOptions,
    });
  } catch (error) {
    error.retryable = true;
    throw error;
  }
  if (response.status === 401) notifySessionExpired(path, requestPolicy);
  const contentType = response.headers.get("Content-Type") || "";
  if (!contentType.includes("application/json")) {
    const error = new Error("api_not_json");
    error.retryable = true;
    error.status = response.status;
    throw error;
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(responseErrorMessage(payload));
    error.status = response.status;
    error.code = payload.error || "request_failed";
    error.workload = payload.workload || "";
    throw error;
  }
  rememberSessionToken(path, payload);
  return payload;
}

export function apiUrl(path) {
  return buildUrl(path);
}

export function authenticatedApiUrl(path) {
  const url = new URL(apiUrl(path), window.location.href);
  const token = window.localStorage.getItem(SESSION_TOKEN_KEY);
  if (token) url.searchParams.set("access_token", token);
  return url.toString();
}

export async function api(path, options = {}) {
  if (/^https?:\/\//i.test(path)) return fetchJson(path, options, "");
  let lastError = null;
  for (const base of backendCandidates()) {
    try {
      const payload = await fetchJson(path, options, base);
      rememberApiBase(base);
      return payload;
    } catch (error) {
      lastError = error;
      if (!error.retryable) break;
    }
  }
  throw lastError || new Error("request_failed");
}

function xhrBinary(path, body, base, onProgress, signal, requestPolicy = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    if (signal?.aborted) return reject(new DOMException("Upload aborted", "AbortError"));
    xhr.open("PUT", buildUrl(path, base), true);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    const token = window.localStorage.getItem(SESSION_TOKEN_KEY);
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded, event.total);
    };
    xhr.onerror = () => reject(Object.assign(new Error("request_failed"), { retryable: true }));
    xhr.onabort = () => reject(new DOMException("Upload aborted", "AbortError"));
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.onload = () => {
      const contentType = xhr.getResponseHeader("Content-Type") || "";
      if (xhr.status === 401) notifySessionExpired(path, requestPolicy);
      if (!contentType.includes("application/json")) return reject(Object.assign(new Error("api_not_json"), { retryable: true, status: xhr.status }));
      const payload = JSON.parse(xhr.responseText || "{}");
      if (xhr.status < 200 || xhr.status >= 300) {
        const error = new Error(responseErrorMessage(payload));
        error.status = xhr.status;
        error.code = payload.error || "request_failed";
        error.workload = payload.workload || "";
        error.retryable = xhr.status === 429 || xhr.status >= 500;
        return reject(error);
      }
      resolve(payload);
    };
    xhr.send(body);
  });
}

export async function uploadChunkApi(path, blob, onProgress, options = {}) {
  let lastError = null;
  for (const base of backendCandidates()) {
    try {
      const payload = await xhrBinary(path, blob, base, onProgress, options.signal, options);
      rememberApiBase(base);
      return payload;
    } catch (error) {
      lastError = error;
      if (!error.retryable) break;
    }
  }
  throw lastError || new Error("request_failed");
}
