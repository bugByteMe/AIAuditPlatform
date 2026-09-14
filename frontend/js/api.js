const API_BASE_KEY = "aiAuditApiBase";
const SESSION_TOKEN_KEY = "aiAuditSessionToken";
const DEFAULT_BACKEND_BASE = "http://127.0.0.1:8765";

function unique(values) {
  return [...new Set(values.filter((value) => value !== null && value !== undefined))];
}

function currentApiBase() {
  return window.localStorage.getItem(API_BASE_KEY) || "";
}

function backendCandidates() {
  const currentBase = `${window.location.protocol}//${window.location.host}`;
  const candidates = [currentApiBase(), ""];
  if (currentBase !== DEFAULT_BACKEND_BASE) candidates.push(DEFAULT_BACKEND_BASE);
  return unique(candidates);
}

function buildUrl(path, base = currentApiBase()) {
  if (/^https?:\/\//i.test(path)) return path;
  if (!base) return path;
  return `${base}${path.startsWith("/") ? path : `/${path}`}`;
}

function rememberApiBase(base) {
  if (base) window.localStorage.setItem(API_BASE_KEY, base);
}

function rememberSessionToken(path, payload) {
  if (path === "/api/login" && payload.sessionToken) {
    window.localStorage.setItem(SESSION_TOKEN_KEY, payload.sessionToken);
  }
  if (path === "/api/logout") {
    window.localStorage.removeItem(SESSION_TOKEN_KEY);
  }
}

async function fetchJson(path, options, base) {
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
  }
  const token = window.localStorage.getItem(SESSION_TOKEN_KEY);
  if (token) headers.Authorization = headers.Authorization || `Bearer ${token}`;
  let response;
  try {
    response = await fetch(buildUrl(path, base), {
      credentials: "include",
      headers,
      ...options,
    });
  } catch (error) {
    error.retryable = true;
    throw error;
  }
  const contentType = response.headers.get("Content-Type") || "";
  if (!contentType.includes("application/json")) {
    const error = new Error("api_not_json");
    error.retryable = true;
    error.status = response.status;
    throw error;
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.message || payload.error || "request_failed");
    error.status = response.status;
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
