import React, { startTransition, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { createDashboardSync, createRevisionGate, topicForPath } from "./dashboardSync.js";

const API_BASE = window.location.origin.includes(":8000") ? "" : "http://127.0.0.1:8000";
const SITE_OPTIONS = [
  { value: "mediamarkt", label: "MediaMarkt" },
  { value: "dreamland", label: "Dreamland" },
  { value: "bol", label: "Bol" },
  { value: "pocketgames", label: "PocketGames" },
];
const SITE_LABELS = Object.fromEntries(SITE_OPTIONS.map((site) => [site.value, site.label]));
const WATCHLIST_SITE_DEFAULTS = {
  mediamarkt: { interval_seconds: 8, max_concurrency: 1, request_timeout_seconds: 12, jitter_seconds: 0.5 },
  dreamland: { interval_seconds: 12, max_concurrency: 2, request_timeout_seconds: 12, jitter_seconds: 0.5 },
  bol: { interval_seconds: 12, max_concurrency: 1, request_timeout_seconds: 12, jitter_seconds: 0.5 },
  pocketgames: { interval_seconds: 8, max_concurrency: 2, request_timeout_seconds: 12, jitter_seconds: 0.5 },
};
const PAGE_ITEMS = [
  { key: "dashboard", label: "Dashboard", caption: "Runtime, heartbeat, overview" },
  { key: "runtime", label: "Runtime", caption: "Queue, timer, action mode" },
  { key: "scan-settings", label: "Scan Settings", caption: "Real parser timing controls" },
  { key: "watchlist", label: "Watchlist", caption: "Priority product tracker" },
  { key: "filters", label: "Filters", caption: "SQLite-backed rules" },
  { key: "logs", label: "Logs", caption: "Error, heartbeat, success" },
  { key: "settings", label: "Settings", caption: "Credentials, proxy, Telegram" },
  { key: "faq", label: "FAQ", caption: "Owner-friendly guidance" },
];
const EMPTY_FILTER = {
  id: null,
  name: "",
  sites: [],
  keywordGroupsText: "",
  excludeWordsText: "",
  minPrice: "",
  maxPrice: "",
  softPrice: true,
  enabled: true,
};
const FAQ_ITEMS = [
  {
    title: "How filters work",
    body: "Each filter rule can target one or more sites, requires one or more keyword groups, and can optionally limit price ranges. A line in the keyword groups box is one AND-group, while commas inside the line act like OR words inside that group.",
  },
  {
    title: "How parsers work",
    body: "MediaMarkt, Dreamland, Bol, and PocketGames scan concurrently during discovery. Side-effect flows such as Selenium checkout stay centralized through one controlled queue and worker.",
  },
  {
    title: "How credentials work",
    body: "The credentials editor writes real values back into the .env file under superparser_modular. Changes that affect runtime behavior can trigger a runtime restart when the system is already running.",
  },
  {
    title: "How Telegram works",
    body: "Telegram settings store the bot token and chat id in the .env file. Heartbeat, success, and error delivery all respect the notification switches before messages are sent.",
  },
  {
    title: "How proxy works",
    body: "The proxy panel stores one global proxy configuration. Scan and fetch HTTP traffic can reuse that proxy, and the dashboard includes a proxy test action so the owner can validate it before running.",
  },
  {
    title: "How run timer works",
    body: "The timer can launch scheduled one-shot scan cycles on an interval. Continuous runtime mode still exists for always-on scanning, and the timer pauses while a continuous run is active.",
  },
  {
    title: "How scan settings work",
    body: "Scan Settings save real parser timing values on the backend. Lower delays increase the chance of rate limits, while Selenium checkout stays serialized and separate from parser scan concurrency.",
  },
  {
    title: "How to read error, heartbeat, and success logs",
    body: "Error logs highlight parser crashes, denies, and action failures. Heartbeat logs summarize runtime health and per-site state. Success logs show good scan completions, matches, and Selenium job finishes.",
  },
];
const THEME_STORAGE_KEY = "pokemon-parser-theme";
const DASHBOARD_REQUEST_TIMEOUT_MS = 12000;
const DASHBOARD_CLEAN_SLATE_HEADER = "X-Dashboard-Clean-Slate";
let cleanSlatePromise = null;
const dashboardSync = createDashboardSync();

function isDashboardApiPath(path) {
  return String(path || "").startsWith("/api/");
}

function isAbortError(error) {
  return error?.name === "AbortError" || String(error?.message || "").toLowerCase().includes("abort");
}

function responseRequestsCleanSlate(response) {
  return response.status === 431 || response.headers.get(DASHBOARD_CLEAN_SLATE_HEADER) === "true";
}

function clearDashboardClientState() {
  try {
    window.localStorage.clear();
  } catch (error) {
    // Browser privacy modes can block storage access.
  }
  try {
    window.sessionStorage.clear();
  } catch (error) {
    // Browser privacy modes can block storage access.
  }
  try {
    const hostname = window.location.hostname;
    const domains = ["", hostname, hostname && !hostname.startsWith(".") ? `.${hostname}` : ""].filter(Boolean);
    const cookieNames = document.cookie
      .split(";")
      .map((cookie) => cookie.split("=")[0].trim())
      .filter(Boolean);
    cookieNames.forEach((name) => {
      document.cookie = `${name}=; Max-Age=0; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Lax`;
      domains.forEach((domain) => {
        document.cookie = `${name}=; Max-Age=0; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; domain=${domain}; SameSite=Lax`;
      });
    });
  } catch (error) {
    // Cookie deletion is best-effort; Clear-Site-Data is also requested from the backend.
  }
}

async function runDashboardCleanSlate() {
  if (cleanSlatePromise) {
    return cleanSlatePromise;
  }
  cleanSlatePromise = Promise.resolve()
    .then(async () => {
      clearDashboardClientState();
      try {
        await fetch(`${API_BASE}/api/system/dashboard-clean-slate`, {
          method: "POST",
          cache: "no-store",
          keepalive: true,
        });
      } catch (error) {
        // Local storage/cookie cleanup has already run; the API call only asks the browser for Clear-Site-Data too.
      }
    })
    .finally(() => {
      cleanSlatePromise = null;
    });
  return cleanSlatePromise;
}

async function request(path, options = {}, retryingAfterCleanSlate = false) {
  const { headers, signal, timeoutMs, cleanSlateOnTimeout: cleanSlateOnTimeoutOption, ...fetchOptions } = options;
  const controller = new AbortController();
  const method = String(fetchOptions.method || "GET").toUpperCase();
  const requestTimeoutMs = Math.max(1000, Number(timeoutMs ?? (method === "GET" ? DASHBOARD_REQUEST_TIMEOUT_MS : 30000)));
  // A slow backend is not evidence of corrupt browser state. Cleanup is only
  // performed when the backend explicitly requests it (or returns HTTP 431).
  const cleanSlateOnTimeout = cleanSlateOnTimeoutOption ?? false;
  const timeoutId = window.setTimeout(() => controller.abort(), requestTimeoutMs);
  if (signal) {
    signal.addEventListener("abort", () => controller.abort(), { once: true });
  }

  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...fetchOptions,
      headers: {
        "Content-Type": "application/json",
        ...(headers ?? {}),
      },
      signal: controller.signal,
    });
  } catch (error) {
    if (cleanSlateOnTimeout && isAbortError(error) && !retryingAfterCleanSlate) {
      await runDashboardCleanSlate();
      return request(path, options, true);
    }
    if (cleanSlateOnTimeout && isAbortError(error)) {
      throw new Error(`Dashboard API timed out after ${requestTimeoutMs} ms while loading ${path}. Browser state was cleared; retry the dashboard.`);
    }
    if (isAbortError(error)) {
      throw new Error(`Dashboard API timed out after ${requestTimeoutMs} ms while loading ${path}.`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }

  if (isDashboardApiPath(path) && responseRequestsCleanSlate(response)) {
    await runDashboardCleanSlate();
    if (!retryingAfterCleanSlate) {
      return request(path, options, true);
    }
  }

  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `Request failed with status ${response.status}`);
  }

  const contentType = response.headers.get("content-type") || "";
  const body = await response.text();
  if (!contentType.includes("application/json")) {
    const looksLikeHtml = body.trim().toLowerCase().startsWith("<!doctype") || body.trim().startsWith("<html");
    if (looksLikeHtml && path.startsWith("/api/")) {
      throw new Error("Backend route returned the dashboard HTML. Restart the backend so the latest API routes are loaded.");
    }
    throw new Error(`Expected JSON but received ${contentType || "unknown content type"}.`);
  }

  const payload = body ? JSON.parse(body) : null;
  if (method !== "GET" && method !== "HEAD") {
    dashboardSync.publish(topicForPath(path), { method, path: String(path).split("?")[0] });
  }
  return payload;
}

function useDashboardReconciliation(topics, onReconcile) {
  const callbackRef = useRef(onReconcile);
  callbackRef.current = onReconcile;
  const topicKey = [...topics].sort().join("|");

  useEffect(() => {
    const accepted = new Set(topicKey.split("|").filter(Boolean));
    const reconcile = () => void callbackRef.current?.();
    const unsubscribe = dashboardSync.subscribe((message) => {
      if (accepted.has(message.topic) || accepted.has("*")) {
        reconcile();
      }
    });
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        reconcile();
      }
    };
    window.addEventListener("focus", reconcile);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      unsubscribe();
      window.removeEventListener("focus", reconcile);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [topicKey]);
}

function useRevisionGate() {
  const gateRef = useRef(null);
  if (gateRef.current === null) {
    gateRef.current = createRevisionGate();
  }
  return gateRef.current;
}

function fallbackLogsSummary(message) {
  return {
    counts: { info: 0, warning: 0, error: 0 },
    tail: [],
    cached: false,
    stale: true,
    warning: message || "Log summary is temporarily unavailable.",
  };
}

const api = {
  getRuntimeStatus: () => request("/api/runtime/status"),
  runRuntime: () => request("/api/runtime/run", { method: "POST" }),
  stopRuntime: () => request("/api/runtime/stop", { method: "POST" }),
  restartRuntime: () => request("/api/runtime/restart", { method: "POST" }),
  getRuntimeOverview: () => request("/api/runtime/overview"),
  getRuntimeTimer: () => request("/api/runtime/timer"),
  saveRuntimeTimer: (payload) =>
    request("/api/runtime/timer", { method: "POST", body: JSON.stringify(payload) }),
  getActionMode: () => request("/api/runtime/action-mode"),
  saveActionMode: (payload) =>
    request("/api/runtime/action-mode", { method: "POST", body: JSON.stringify(payload) }),
  getScanSettings: () => request("/api/scan-settings"),
  getEffectiveScanSettings: () => request("/api/scan-settings/effective"),
  saveScanSettings: (payload) =>
    request("/api/scan-settings", { method: "POST", body: JSON.stringify(payload) }),
  resetScanSettings: () => request("/api/scan-settings/reset-defaults", { method: "POST" }),
  getParsers: () => request("/api/parsers"),
  toggleParser: (site) => request(`/api/parsers/${site}/toggle`, { method: "POST" }),
  getFilters: () => request("/api/filters"),
  getFilter: (filterId) => request(`/api/filters/${filterId}`),
  createFilter: (payload) => request("/api/filters", { method: "POST", body: JSON.stringify(payload) }),
  updateFilter: (filterId, payload) =>
    request(`/api/filters/${filterId}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteFilter: (filterId) => request(`/api/filters/${filterId}`, { method: "DELETE" }),
  toggleFilter: (filterId) => request(`/api/filters/${filterId}/toggle`, { method: "POST" }),
  getCredentials: () => request("/api/credentials"),
  saveCredentials: (values) =>
    request("/api/credentials", { method: "POST", body: JSON.stringify({ values }) }),
  reloadCredentials: () => request("/api/credentials/reload", { method: "POST" }),
  getConfigStatus: () => request("/api/status/config"),
  getLogsSummary: () =>
    request("/api/logs/summary", { timeoutMs: 3500, cleanSlateOnTimeout: false }),
  getLogsSummarySafe: async () => {
    try {
      return await api.getLogsSummary();
    } catch (error) {
      return fallbackLogsSummary(error instanceof Error ? error.message : "Log summary is temporarily unavailable.");
    }
  },
  getLogs: ({ type, site, q, limit = 200 }) =>
    request(
      `/api/logs?type=${encodeURIComponent(type || "")}&site=${encodeURIComponent(site || "")}&q=${encodeURIComponent(q || "")}&limit=${encodeURIComponent(limit)}`,
    ),
  getProxy: () => request("/api/proxy"),
  saveProxy: (payload) => request("/api/proxy", { method: "POST", body: JSON.stringify(payload) }),
  testProxy: (payload) => request("/api/proxy/test", { method: "POST", body: JSON.stringify(payload) }),
  getTelegram: () => request("/api/telegram"),
  saveTelegram: (payload) => request("/api/telegram", { method: "POST", body: JSON.stringify(payload) }),
  testTelegram: (payload) =>
    request("/api/telegram/test", { method: "POST", body: JSON.stringify(payload) }),
  getNotifications: () => request("/api/notifications"),
  saveNotifications: (payload) =>
    request("/api/notifications", { method: "POST", body: JSON.stringify(payload) }),
  getWorkerSettings: () => request("/api/worker-settings"),
  saveWorkerSettings: (payload) =>
    request("/api/worker-settings", { method: "POST", body: JSON.stringify(payload) }),
  exportSettingsBackup: async () => {
    const response = await fetch(`${API_BASE}/api/settings-backup/export`);
    if (!response.ok) {
      throw new Error((await response.text()) || `Request failed with status ${response.status}`);
    }
    return {
      blob: await response.blob(),
      filename: filenameFromContentDisposition(
        response.headers.get("content-disposition"),
        buildSettingsBackupFilename(),
      ),
    };
  },
  previewSettingsRestore: (payload) =>
    request("/api/settings-backup/preview", { method: "POST", body: JSON.stringify(payload) }),
  restoreSettingsBackup: (payload) =>
    request("/api/settings-backup/restore", { method: "POST", body: JSON.stringify(payload) }),
  saveSettingsSnapshot: () => request("/api/settings-backup/snapshot", { method: "POST" }),
  createChromeProfile: () => request("/api/chrome-profile/create", { method: "POST" }),
  getDbStatus: () => request("/api/db/status"),
  backupDb: () => request("/api/db/backup", { method: "POST" }),
  clearOldLogs: (days) =>
    request("/api/db/clear-old-logs", { method: "POST", body: JSON.stringify({ days }) }),
  clearStaleActions: (days) =>
    request("/api/db/clear-stale-actions", { method: "POST", body: JSON.stringify({ days }) }),
  getWatchlist: () => request("/api/watchlist"),
  getWatchlistSummary: () => request("/api/watchlist/summary"),
  getWatchlistState: () => request("/api/watchlist/state"),
  buildWatchlist: () => request("/api/watchlist/build-from-filters", { method: "POST", body: JSON.stringify({}) }),
  syncWatchlist: () => request("/api/watchlist/sync-now", { method: "POST", body: JSON.stringify({}) }),
  syncWatchlistItem: (id) => request(`/api/watchlist/${id}/sync-now`, { method: "POST" }),
  patchWatchlistItem: (id, payload) =>
    request(`/api/watchlist/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteWatchlistItem: (id) => request(`/api/watchlist/${id}`, { method: "DELETE" }),
  addManualWatchlistItem: (payload) =>
    request("/api/watchlist/manual", { method: "POST", body: JSON.stringify(payload) }),
};

function formatDate(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function padDatePart(value) {
  return String(value).padStart(2, "0");
}

function buildSettingsBackupFilename() {
  const now = new Date();
  return `pokemon_parser_settings_backup_${now.getFullYear()}-${padDatePart(now.getMonth() + 1)}-${padDatePart(now.getDate())}_${padDatePart(now.getHours())}-${padDatePart(now.getMinutes())}.json`;
}

function filenameFromContentDisposition(value, fallback) {
  const match = /filename="?([^"]+)"?/i.exec(value || "");
  return match?.[1] || fallback;
}

function saveBlobAsFile(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function formatSeconds(value) {
  const seconds = Math.max(0, Number(value || 0));
  if (!seconds) {
    return "0s";
  }
  if (seconds < 60) {
    return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = Math.floor(seconds % 60);
  if (hours) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m ${rest}s`;
}

function toTimerString(seconds) {
  const value = Math.max(0, Math.floor(seconds || 0));
  const hours = String(Math.floor(value / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((value % 3600) / 60)).padStart(2, "0");
  const rest = String(value % 60).padStart(2, "0");
  return `${hours}:${minutes}:${rest}`;
}

function statusTone(status) {
  if (["error", "failed"].includes(status)) {
    return "bad";
  }
  if (["parse_unknown", "challenge_or_blocked", "rate_limited_unknown", "http_429_throttled"].includes(status)) {
    return "bad";
  }
  if (["out_of_stock", "unavailable", "not_found_currently", "notify_only", "soon_available"].includes(status)) {
    return "warn";
  }
  if (["in_stock", "add_to_cart_available", "delivery_available", "offer_available", "variant_available"].includes(status)) {
    return "live";
  }
  if (["cooldown", "warning", "partial_success"].includes(status)) {
    return "warn";
  }
  if (["scanning", "running", "busy", "recovered_via_fallback"].includes(status)) {
    return "live";
  }
  return "ok";
}

function mediamarktWatchlistMessage(diagnostics = {}) {
  if (diagnostics.last_action_target_exists && diagnostics.buyable_marker_found) {
    return "Buy button found, action target ready";
  }
  if (diagnostics.alert_notify_marker_found) {
    return "Notify-only / soon available, no checkout action";
  }
  if (diagnostics.last_status === "parse_unknown") {
    return "Parse unknown, diagnostic snapshot saved";
  }
  if (diagnostics.last_status === "rate_limited_unknown" || diagnostics.rate_limited_count > 0) {
    return "Rate limited, checkout action held";
  }
  return diagnostics.last_status ? "Last MediaMarkt Watchlist check recorded" : "No MediaMarkt Watchlist check yet";
}

function seleniumRuntimeMessage(selenium = {}) {
  if (selenium.driver_exists && selenium.prewarmed) {
    return "Selenium warm browser active";
  }
  if (selenium.driver_exists) {
    return "Selenium browser active";
  }
  if (selenium.lifecycle_state === "starting") {
    return "Selenium browser starting";
  }
  return "Selenium browser closed";
}

function seleniumPrewarmMessage(selenium = {}) {
  const config = selenium.config ?? {};
  if (!config.prewarm_enabled) {
    return "Prewarm disabled";
  }
  if (!config.prewarm_on_runtime_start) {
    return "Prewarm skipped on runtime start";
  }
  if (selenium.driver_exists && selenium.prewarmed) {
    return "Prewarm ready";
  }
  if (selenium.last_prewarm_error) {
    return "Prewarm failed";
  }
  if (selenium.last_prewarm_skip_reason) {
    return `Prewarm skipped: ${selenium.last_prewarm_skip_reason}`;
  }
  return "Prewarm waiting for browser";
}

function warmTabsMessage(selenium = {}) {
  const tabs = selenium.warm_tabs ?? [];
  const ready = tabs.filter((tab) => tab.warm_state === "ready").length;
  const max = selenium.warm_tabs_max ?? 0;
  if (!selenium.warm_tabs_enabled) {
    return "Watchlist warm tabs disabled";
  }
  return `MediaMarkt warm tabs: ${ready}/${max} ready`;
}

function watchlistItemDiagnostics(item = {}) {
  const extra = item.extra ?? {};
  const resultExtra = extra.result_extra ?? {};
  const diagnostic = extra.pdp_diagnostic ?? resultExtra.pdp_diagnostic ?? {};
  return {
    last_status: item.current_inventory_status,
    last_endpoint: extra.source_endpoint,
    last_http_status: extra.http_status,
    last_confidence: item.status_confidence_score,
    last_action_target_exists: Boolean(extra.action_target_exists ?? diagnostic.action_target_exists),
    last_skip_reason: extra.skip_reason,
    buyable_marker_found: Boolean(
      extra.buyable_marker_found ||
        resultExtra.buyable_marker_found ||
        diagnostic.add_to_cart_button_found ||
        diagnostic.delivery_available_marker ||
        diagnostic.online_status_available_marker,
    ),
    alert_notify_marker_found: Boolean(
      extra.alert_notify_marker_found ||
        resultExtra.alert_notify_marker_found ||
        diagnostic.alert_button_found ||
        diagnostic.notify_text_found ||
        diagnostic.soon_available_text_found,
    ),
    last_diagnostic_summary: diagnostic,
  };
}

function filterToDraft(item) {
  if (!item) {
    return { ...EMPTY_FILTER };
  }
  return {
    id: item.id,
    name: item.name ?? "",
    sites: item.sites ?? [],
    keywordGroupsText: (item.keyword_groups ?? []).map((group) => group.join(", ")).join("\n"),
    excludeWordsText: (item.exclude_words ?? []).join(", "),
    minPrice: item.min_price ?? "",
    maxPrice: item.max_price ?? "",
    softPrice: item.soft_price ?? true,
    enabled: item.enabled ?? true,
  };
}

function draftToPayload(draft) {
  return {
    name: draft.name.trim(),
    sites: draft.sites,
    keyword_groups: draft.keywordGroupsText
      .split("\n")
      .map((line) => line.split(",").map((value) => value.trim()).filter(Boolean))
      .filter((group) => group.length),
    exclude_words: draft.excludeWordsText
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
    min_price: draft.minPrice === "" ? null : Number(draft.minPrice),
    max_price: draft.maxPrice === "" ? null : Number(draft.maxPrice),
    soft_price: Boolean(draft.softPrice),
    enabled: Boolean(draft.enabled),
  };
}

function formatSiteList(sites) {
  if (!sites?.length) {
    return "All sites";
  }
  return sites.map((site) => SITE_LABELS[site] ?? site).join(", ");
}

function groupCredentials(items) {
  return items.reduce((accumulator, item) => {
    if (!accumulator[item.group]) {
      accumulator[item.group] = [];
    }
    accumulator[item.group].push(item);
    return accumulator;
  }, {});
}

function buildScanSettingsDraft(payload) {
  const sites = Object.fromEntries(
    SITE_OPTIONS.map((site) => {
      const current = payload?.sites?.[site.value] ?? {};
      return [
        site.value,
        {
          enabled: Boolean(current.enabled),
          scan_delay_seconds: current.scan_delay_seconds ?? "",
          cooldown_seconds: current.cooldown_seconds ?? "",
          request_timeout_seconds: current.request_timeout_seconds ?? "",
          max_pages: current.max_pages ?? "",
          page_delay_seconds: current.page_delay_seconds ?? "",
          label: current.label ?? site.label,
          supports_max_pages: Boolean(current.supports_max_pages),
          supports_page_delay_seconds: current.supports_page_delay_seconds ?? true,
          uses_saved_scan_delay: Boolean(current.uses_saved_scan_delay),
        },
      ];
    }),
  );
  const watchlistSites = Object.fromEntries(
    SITE_OPTIONS.map((site) => {
      const defaults = WATCHLIST_SITE_DEFAULTS[site.value];
      const current = payload?.watchlist?.sites?.[site.value] ?? {};
      return [
        site.value,
        {
          enabled: current.enabled ?? true,
          interval_seconds: current.interval_seconds ?? defaults.interval_seconds,
          max_concurrency: current.max_concurrency ?? defaults.max_concurrency,
          request_timeout_seconds: current.request_timeout_seconds ?? defaults.request_timeout_seconds,
          jitter_seconds: current.jitter_seconds ?? defaults.jitter_seconds,
          discovery_scan_delay_seconds:
            current.discovery_scan_delay_seconds ?? sites[site.value]?.scan_delay_seconds ?? "",
          label: current.label ?? site.label,
        },
      ];
    }),
  );

  return {
    global: {
      scan_interval_seconds: payload?.global?.scan_interval_seconds ?? "",
      max_parallel_parsers: payload?.global?.max_parallel_parsers ?? 4,
      effective_parallel_parsers: payload?.global?.effective_parallel_parsers ?? 0,
      request_timeout_seconds: payload?.global?.request_timeout_seconds ?? 20,
      retry_delay_seconds: payload?.global?.retry_delay_seconds ?? 0.35,
      max_retries: payload?.global?.max_retries ?? 2,
    },
    sites,
    watchlist: {
      enabled: payload?.watchlist?.enabled ?? true,
      backoff_on_429: payload?.watchlist?.backoff_on_429 ?? true,
      backoff_multiplier: payload?.watchlist?.backoff_multiplier ?? 2,
      max_backoff_seconds: payload?.watchlist?.max_backoff_seconds ?? 300,
      pause_site_on_error: payload?.watchlist?.pause_site_on_error ?? false,
      sites: watchlistSites,
    },
    storage: payload?.storage ?? null,
    effective: payload?.effective ?? null,
  };
}

function resolveInitialTheme() {
  if (typeof window === "undefined") {
    return "dark";
  }
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === "dark" || stored === "light") {
    return stored;
  }
  return "dark";
}

function toScanSettingsPayload(draft) {
  return {
    global: {
      scan_interval_seconds:
        draft.global.scan_interval_seconds === "" ? null : Number(draft.global.scan_interval_seconds),
      max_parallel_parsers: Number(draft.global.max_parallel_parsers) || 1,
      request_timeout_seconds: Number(draft.global.request_timeout_seconds) || 20,
      retry_delay_seconds: Number(draft.global.retry_delay_seconds) || 0,
      max_retries: Number(draft.global.max_retries) || 0,
    },
    sites: Object.fromEntries(
      SITE_OPTIONS.map((site) => [
        site.value,
        {
          enabled: Boolean(draft.sites[site.value]?.enabled),
          scan_delay_seconds: Number(draft.sites[site.value]?.scan_delay_seconds) || 0,
          cooldown_seconds: Number(draft.sites[site.value]?.cooldown_seconds) || 0,
          request_timeout_seconds: Number(draft.sites[site.value]?.request_timeout_seconds) || 0,
          max_pages:
            draft.sites[site.value]?.max_pages === "" ? null : Number(draft.sites[site.value]?.max_pages),
          page_delay_seconds: Number(draft.sites[site.value]?.page_delay_seconds) || 0,
        },
      ]),
    ),
    watchlist: {
      enabled: Boolean(draft.watchlist?.enabled),
      backoff_on_429: Boolean(draft.watchlist?.backoff_on_429),
      backoff_multiplier: Number(draft.watchlist?.backoff_multiplier) || 2,
      max_backoff_seconds: Number(draft.watchlist?.max_backoff_seconds) || 300,
      pause_site_on_error: Boolean(draft.watchlist?.pause_site_on_error),
      sites: Object.fromEntries(
        SITE_OPTIONS.map((site) => {
          const current = draft.watchlist?.sites?.[site.value] ?? WATCHLIST_SITE_DEFAULTS[site.value];
          return [
            site.value,
            {
              enabled: Boolean(current.enabled),
              interval_seconds: Number(current.interval_seconds) || 1,
              max_concurrency: Number(current.max_concurrency) || 1,
              request_timeout_seconds: Number(current.request_timeout_seconds) || 1,
              jitter_seconds: Number(current.jitter_seconds) || 0,
            },
          ];
        }),
      ),
    },
  };
}

function Shell({ activePage, onSelect, theme, onToggleTheme, children }) {
  return (
    <div className="appShell">
      <aside className="sidebar">
        <div className="brandPanel">
          <span className="brandBadge">P2</span>
          <div>
            <p className="eyebrow">Concurrent runtime</p>
            <h1>Pokemon Parser Dashboard</h1>
          </div>
        </div>
        <nav className="navStack">
          {PAGE_ITEMS.map((page) => (
            <button
              key={page.key}
              className={`navButton ${page.key === activePage ? "is-active" : ""}`}
              onClick={() => onSelect(page.key)}
              type="button"
            >
              <strong>{page.label}</strong>
              <small>{page.caption}</small>
            </button>
          ))}
        </nav>
        <button className="button ghost themeToggle" onClick={onToggleTheme} type="button">
          Switch to {theme === "dark" ? "Light" : "Dark"} Theme
        </button>
        <div className="sidebarNote">
          <span className="statusPill tone-live">UI2 branch</span>
          <p>Dreamland is treated as a first-class parser everywhere.</p>
        </div>
      </aside>
      <main className="mainCanvas">{children}</main>
    </div>
  );
}

function MetricCard({ label, value, caption, tone = "ok" }) {
  return (
    <article className={`metricCard tone-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <p>{caption}</p>
    </article>
  );
}

function Panel({ title, eyebrow, action, children, className = "" }) {
  return (
    <section className={`panel ${className}`.trim()}>
      <div className="panelHeader">
        <div>
          {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
          <h3>{title}</h3>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function LogPreview({ title, items, emptyText }) {
  return (
    <div className="logPreview">
      <div className="subHeader">
        <h4>{title}</h4>
      </div>
      <div className="logStack">
        {items.length ? (
          items.map((item) => (
            <article key={`${item.id ?? item.timestamp}-${item.message}`} className="logCard">
              <div className="logMeta">
                <span className={`statusPill tone-${statusTone(item.category || item.level?.toLowerCase())}`}>
                  {item.category || item.level}
                </span>
                <span>{item.site || "system"}</span>
                <span>{formatDate(item.timestamp)}</span>
              </div>
              <p>{item.message}</p>
            </article>
          ))
        ) : (
          <p className="emptyState">{emptyText}</p>
        )}
      </div>
    </div>
  );
}

function RuntimeWarnings({ warnings }) {
  const items = (warnings ?? []).filter(Boolean);
  if (!items.length) {
    return null;
  }

  return (
    <div className="alert warning" role="alert">
      {items.map((warning) => (
        <p key={warning}>{warning}</p>
      ))}
    </div>
  );
}

function DashboardPage() {
  const revisionGate = useRevisionGate();
  const [reconcileRevision, setReconcileRevision] = useState(0);
  const [state, setState] = useState({
    loading: true,
    error: "",
    runtimeStatus: null,
    overview: null,
    parsers: [],
    config: null,
    logsSummary: null,
    errors: [],
    heartbeats: [],
    successes: [],
    notifications: null,
    db: null,
  });
  const [busyAction, setBusyAction] = useState("");
  const [runtimeNotice, setRuntimeNotice] = useState("");

  useDashboardReconciliation(
    ["runtime", "parsers", "filters", "watchlist", "settings", "credentials", "proxy", "telegram", "scan-settings", "logs"],
    () => setReconcileRevision((current) => current + 1),
  );

  useEffect(() => {
    let cancelled = false;

    async function loadData(isBackground = false) {
      const revision = revisionGate.issue();
      try {
        const [
          runtimeStatus,
          overview,
          parsers,
          config,
          logsSummary,
          errors,
          heartbeats,
          successes,
          notifications,
          db,
        ] = await Promise.all([
          api.getRuntimeStatus(),
          api.getRuntimeOverview(),
          api.getParsers(),
          api.getConfigStatus(),
          api.getLogsSummarySafe(),
          api.getLogs({ type: "error", limit: 4 }),
          api.getLogs({ type: "heartbeat", limit: 4 }),
          api.getLogs({ type: "success", limit: 4 }),
          api.getNotifications(),
          api.getDbStatus(),
        ]);

        if (cancelled || !revisionGate.isLatest(revision)) {
          return;
        }

        startTransition(() => {
          setState({
            loading: false,
            error: "",
            runtimeStatus,
            overview,
            parsers: parsers.items ?? [],
            config,
            logsSummary,
            errors: errors.items ?? [],
            heartbeats: heartbeats.items ?? [],
            successes: successes.items ?? [],
            notifications,
            db,
          });
        });
      } catch (error) {
        if (cancelled || !revisionGate.isLatest(revision)) {
          return;
        }
        setState((current) => ({
          ...current,
          loading: isBackground ? current.loading : false,
          error: error instanceof Error ? error.message : "Failed to load dashboard.",
        }));
      }
    }

    loadData();
    const timer = window.setInterval(() => {
      void loadData(true);
    }, 5000);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [reconcileRevision, revisionGate]);

  async function handleRuntimeAction(action) {
    if (busyAction) {
      return;
    }
    if (action === "run" && state.runtimeStatus?.running) {
      setRuntimeNotice("Runtime already running.");
      return;
    }

    setRuntimeNotice("");
    setBusyAction(action);
    try {
      let actionResult = null;
      if (action === "run") {
        actionResult = await api.runRuntime();
      } else if (action === "stop") {
        actionResult = await api.stopRuntime();
      } else if (action === "restart") {
        actionResult = await api.restartRuntime();
      } else if (action === "backup") {
        actionResult = await api.backupDb();
      }
      const [runtimeStatus, overview, parsers, config, logsSummary] = await Promise.all([
        api.getRuntimeStatus(),
        api.getRuntimeOverview(),
        api.getParsers(),
        api.getConfigStatus(),
        api.getLogsSummarySafe(),
      ]);
      setState((current) => ({
        ...current,
        runtimeStatus,
        overview,
        parsers: parsers.items ?? [],
        config,
        logsSummary,
        error: "",
      }));
      if (actionResult?.message) {
        setRuntimeNotice(actionResult.message);
      } else if (action === "stop") {
        setRuntimeNotice("Runtime stopped and Selenium shutdown was requested.");
      }
    } catch (error) {
      setState((current) => ({
        ...current,
        error: error instanceof Error ? error.message : `Failed to ${action}.`,
      }));
    } finally {
      setBusyAction("");
    }
  }

  async function handleToggleParser(site) {
    setBusyAction(`parser-${site}`);
    try {
      await api.toggleParser(site);
      const [parsers, overview, config] = await Promise.all([
        api.getParsers(),
        api.getRuntimeOverview(),
        api.getConfigStatus(),
      ]);
      setState((current) => ({
        ...current,
        parsers: parsers.items ?? [],
        overview,
        config,
      }));
    } catch (error) {
      setState((current) => ({
        ...current,
        error: error instanceof Error ? error.message : `Failed to toggle ${site}.`,
      }));
    } finally {
      setBusyAction("");
    }
  }

  const runtimeStatus = state.runtimeStatus;
  const overview = state.overview;
  const activeSites = overview?.active_parsers ?? [];
  const enabledSites = overview?.enabled_parsers ?? [];
  const queueSize = overview?.queue_size ?? 0;
  const selenium = overview?.selenium;
  const scanSettings = overview?.scan_settings;
  const watchlistRuntime = overview?.watchlist ?? {};
  const watchlistTrackerStatus = watchlistRuntime.enabled && watchlistRuntime.running ? "Active" : "Idle";
  const totalWatchlistItems = watchlistRuntime.total_watchlist_items ?? watchlistRuntime.total_enabled_watchlist_items ?? 0;
  const activeWatchlistItems =
    watchlistRuntime.actively_monitored_watchlist_items ?? watchlistRuntime.total_enabled_watchlist_items ?? 0;
  const mediamarktWatchlistDiagnostics = watchlistRuntime.mediamarkt_diagnostics ?? {};
  const seleniumConfig = selenium?.config ?? {};
  const warmTabs = selenium?.warm_tabs ?? [];
  const readyWarmTabs = warmTabs.filter((tab) => tab.warm_state === "ready").length;

  return (
    <div className="pageStack">
      <section className="heroPanel">
        <div>
          <p className="eyebrow">Expanded phase-2 dashboard</p>
          <h2>Control the runtime, inspect heartbeat, and manage the owner-facing system from one place.</h2>
        </div>
        <div className="heroActions">
          <button
            className="button ghost"
            type="button"
            disabled={Boolean(busyAction)}
            onClick={() => void handleRuntimeAction("backup")}
          >
            Backup DB
          </button>
          <button
            className="button primary"
            type="button"
            disabled={Boolean(busyAction) || Boolean(runtimeStatus?.running)}
            onClick={() => void handleRuntimeAction("run")}
          >
            {busyAction === "run" ? "Starting..." : runtimeStatus?.running ? "Runtime Running" : "Start Runtime"}
          </button>
          <button
            className="button"
            type="button"
            disabled={Boolean(busyAction) || !runtimeStatus?.running}
            onClick={() => void handleRuntimeAction("stop")}
          >
            {busyAction === "stop" ? "Stopping..." : "Stop Runtime"}
          </button>
          <button
            className="button"
            type="button"
            disabled={Boolean(busyAction)}
            onClick={() => void handleRuntimeAction("restart")}
          >
            {busyAction === "restart" ? "Restarting..." : "Restart Runtime"}
          </button>
        </div>
      </section>

      {state.error ? <div className="alert error">{state.error}</div> : null}
      {runtimeNotice ? <div className="alert success">{runtimeNotice}</div> : null}
      <RuntimeWarnings warnings={state.overview?.warnings} />
      {state.loading ? <div className="panel">Loading dashboard data...</div> : null}

      <div className="metricGrid">
        <MetricCard
          label="Runtime"
          value={runtimeStatus?.running ? "Running" : "Stopped"}
          caption={runtimeStatus?.running ? `PID ${runtimeStatus.pid}` : "No active runtime loop"}
          tone={runtimeStatus?.running ? "live" : "warn"}
        />
        <MetricCard
          label="Active Parsers"
          value={`${activeSites.length}/${enabledSites.length}`}
          caption={`${enabledSites.length} enabled across MediaMarkt, Dreamland, Bol, and PocketGames`}
          tone={activeSites.length ? "live" : "ok"}
        />
        <MetricCard
          label="Central Queue"
          value={queueSize}
          caption={selenium?.busy ? "Selenium worker is busy" : "Serialized action executor idle"}
          tone={queueSize ? "warn" : "ok"}
        />
        <MetricCard
          label="Run Timer"
          value={overview?.timer?.enabled ? `${overview.timer.interval} ${overview.timer.unit}` : "Disabled"}
          caption={`Next run ${formatDate(overview?.timer?.next_run_at)}`}
          tone={overview?.timer?.enabled ? "ok" : "warn"}
        />
        <MetricCard
          label="Action Mode"
          value={overview?.action_mode ?? state.config?.action_mode ?? "unknown"}
          caption={state.notifications?.enabled ? "Notifications enabled" : "Notifications muted"}
          tone={overview?.action_mode === "selenium" ? "warn" : "ok"}
        />
        <MetricCard
          label="Selenium Warm"
          value={selenium?.driver_exists ? "Active" : "Closed"}
          caption={`${seleniumPrewarmMessage(selenium)}. ${warmTabsMessage(selenium)}`}
          tone={selenium?.driver_exists && selenium?.prewarmed ? "live" : "warn"}
        />
        <MetricCard
          label="Config Health"
          value={state.config?.all_required_ok ? "Healthy" : "Needs Review"}
          caption={`${state.config?.missing_required_fields?.length ?? 0} missing required values`}
          tone={state.config?.all_required_ok ? "ok" : "bad"}
        />
        <MetricCard
          label="Scan Interval"
          value={
            scanSettings?.global?.scan_interval_seconds
              ? formatSeconds(scanSettings.global.scan_interval_seconds)
              : "Legacy"
          }
          caption={
            scanSettings?.global?.scan_interval_seconds
              ? "Full runtime scan cycle cadence"
              : "Per-site timing defaults still apply"
          }
          tone="ok"
        />
        <MetricCard
          label="Parallel Parsers"
          value={scanSettings?.effective?.effective_parallel_parsers ?? scanSettings?.global?.effective_parallel_parsers ?? 0}
          caption={`${scanSettings?.effective?.enabled_sites?.length ?? enabledSites.length} enabled parser(s) can participate`}
          tone="live"
        />
        <MetricCard
          label="Watchlist Tracker"
          value={watchlistTrackerStatus}
          caption={`${totalWatchlistItems} total / ${activeWatchlistItems} monitored`}
          tone={watchlistRuntime.last_error ? "bad" : watchlistRuntime.running ? "live" : "warn"}
        />
        <MetricCard
          label="MediaMarkt PDP"
          value={mediamarktWatchlistDiagnostics.last_status ?? "-"}
          caption={mediamarktWatchlistMessage(mediamarktWatchlistDiagnostics)}
          tone={statusTone(mediamarktWatchlistDiagnostics.last_status)}
        />
      </div>

      <div className="dashboardGrid">
        <Panel title="Parser Overview" eyebrow="Concurrent discovery, centralized action queue" className="span-12">
          <div className="table">
            <div className="tableRow tableHead">
              <span>Site</span>
              <span>Status</span>
              <span>Products</span>
              <span>Events</span>
              <span>Cooldown</span>
              <span>Last Success</span>
              <span>Toggle</span>
            </div>
            {state.parsers.map((item) => (
              <div className="tableRow" key={item.site}>
                <span>
                  <strong>{item.label}</strong>
                  <small>{item.site}</small>
                </span>
                <span>
                  <span className={`statusPill tone-${statusTone(item.status)}`}>{item.status}</span>
                  <small>{item.message || "-"}</small>
                  {item.site === "mediamarkt" ? (
                    <small>
                      GraphQL {item.graphql_circuit_open ? "circuit open" : "active"} / {item.discovery_routing_mode ?? "normal"}
                    </small>
                  ) : null}
                </span>
                <span>
                  {item.active_product_count} listed / {item.product_count} saved
                  <small>
                    {item.in_stock_count ?? 0} in stock / {item.out_of_stock_count ?? 0} out of stock
                  </small>
                </span>
                <span>{item.event_count}</span>
                <span>{formatSeconds(item.cooldown_in_seconds)}</span>
                <span>{formatDate(item.last_success_at)}</span>
                <span>
                  <button
                    className={`toggleButton ${item.enabled ? "is-enabled" : ""}`}
                    onClick={() => void handleToggleParser(item.site)}
                    type="button"
                  >
                    {busyAction === `parser-${item.site}` ? "..." : item.enabled ? "On" : "Off"}
                  </button>
                </span>
              </div>
            ))}
          </div>
        </Panel>

        <div className="statusOverviewGrid span-12">
          <Panel title="Runtime Snapshot" eyebrow="Heartbeat-aware overview">
            <dl className="detailList">
              <div>
                <dt>Uptime</dt>
                <dd>{toTimerString(runtimeStatus?.uptime_seconds ?? 0)}</dd>
              </div>
              <div>
                <dt>Started</dt>
                <dd>{formatDate(runtimeStatus?.started_at)}</dd>
              </div>
              <div>
                <dt>Last Heartbeat</dt>
                <dd>{formatDate(overview?.last_heartbeat_at)}</dd>
              </div>
              <div>
                <dt>Worker</dt>
                <dd>
                  {selenium?.worker_thread_alive ? (selenium.busy ? "Busy" : "Ready") : "Idle"}
                </dd>
              </div>
              <div>
                <dt>Dispatcher</dt>
                <dd>{selenium?.dispatcher_exists ? (selenium.dispatcher_running ? "Running" : "Ready") : "None"}</dd>
              </div>
              <div>
                <dt>Browser</dt>
                <dd>{selenium?.driver_exists ? "Open" : "Closed"}</dd>
              </div>
              <div>
                <dt>Warm Browser</dt>
                <dd>{seleniumRuntimeMessage(selenium)}</dd>
              </div>
              <div>
                <dt>Prewarm Config</dt>
                <dd>
                  {seleniumConfig.prewarm_enabled ? "Enabled" : "Disabled"} /{" "}
                  {seleniumConfig.prewarm_on_runtime_start ? "start" : "manual"}
                </dd>
              </div>
              <div>
                <dt>Prewarm State</dt>
                <dd>{seleniumPrewarmMessage(selenium)}</dd>
              </div>
              <div>
                <dt>Warm Tabs</dt>
                <dd>
                  {readyWarmTabs}/{selenium?.warm_tabs_max ?? 0} ready
                </dd>
              </div>
              <div>
                <dt>Action Path</dt>
                <dd>{selenium?.active_action ? "active checkout" : selenium?.warm_tabs_count ? "warm tab ready" : "cold fallback"}</dd>
              </div>
              <div>
                <dt>First Button Search</dt>
                <dd>{Number(selenium?.last_button_search_latency_seconds ?? 0).toFixed(1)}s</dd>
              </div>
              <div>
                <dt>ChromeDriver PID</dt>
                <dd>{selenium?.chromedriver_pid ?? "-"}</dd>
              </div>
              <div>
                <dt>Watchlist</dt>
                <dd>{watchlistTrackerStatus}</dd>
              </div>
              <div>
                <dt>Watchlist Items</dt>
                <dd>
                  {totalWatchlistItems} total / {activeWatchlistItems} monitored
                </dd>
              </div>
              <div>
                <dt>Watchlist Next</dt>
                <dd>{formatSeconds(watchlistRuntime.next_cycle_in_seconds)}</dd>
              </div>
              <div>
                <dt>MM PDP Check</dt>
                <dd>{mediamarktWatchlistDiagnostics.last_status ?? "-"}</dd>
              </div>
              <div>
                <dt>MM Action Target</dt>
                <dd>{mediamarktWatchlistDiagnostics.last_action_target_exists ? "Ready" : "None"}</dd>
              </div>
              <div>
                <dt>MM Markers</dt>
                <dd>
                  {mediamarktWatchlistDiagnostics.buyable_marker_found ? "Buyable" : "No buyable"} /{" "}
                  {mediamarktWatchlistDiagnostics.alert_notify_marker_found ? "notify" : "no notify"}
                </dd>
              </div>
              <div>
                <dt>Queue</dt>
                <dd>
                  {overview?.dispatcher?.pending?.length ?? 0} pending / {overview?.dispatcher?.running?.length ?? 0} running
                </dd>
              </div>
              <div>
                <dt>DB Path</dt>
                <dd className="breakableText">{state.db?.path ?? "-"}</dd>
              </div>
            </dl>
          </Panel>

          <Panel title="Scan Settings Snapshot" eyebrow="Scheduler and request timing">
            <dl className="detailList">
              <div>
                <dt>Full Scan Interval</dt>
                <dd>
                  {scanSettings?.global?.scan_interval_seconds
                    ? formatSeconds(scanSettings.global.scan_interval_seconds)
                    : "Legacy per-site defaults"}
                </dd>
              </div>
              <div>
                <dt>Max Parallel Parsers</dt>
                <dd>{scanSettings?.global?.max_parallel_parsers ?? "-"}</dd>
              </div>
              <div>
                <dt>Request Timeout</dt>
                <dd>{formatSeconds(scanSettings?.global?.request_timeout_seconds ?? 0)}</dd>
              </div>
              <div>
                <dt>Retry Delay</dt>
                <dd>{formatSeconds(scanSettings?.global?.retry_delay_seconds ?? 0)}</dd>
              </div>
              <div>
                <dt>Max Retries</dt>
                <dd>{scanSettings?.global?.max_retries ?? 0}</dd>
              </div>
              <div>
                <dt>Storage</dt>
                <dd>{scanSettings?.storage?.exists ? "Saved" : "Defaults only"}</dd>
              </div>
            </dl>
          </Panel>

          <Panel title="Notifications" eyebrow="Owner-facing switches">
            <div className="toggleList">
              {[
                ["Notifications", state.notifications?.enabled],
                ["Heartbeat alerts", state.notifications?.heartbeat_alerts],
                ["Success alerts", state.notifications?.success_alerts],
                ["Error alerts", state.notifications?.error_alerts],
              ].map(([label, active]) => (
                <div className="toggleRow" key={label}>
                  <span>{label}</span>
                  <span className={`statusPill tone-${active ? "ok" : "warn"}`}>
                    {active ? "On" : "Off"}
                  </span>
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="Config Validation" eyebrow="Real env and parser checks">
            <div className="validationList">
              {(state.config?.checks ?? []).map((item) => (
                <div className="validationRow" key={item.key}>
                  <span className="validationLabel">
                    <span className={`statusDot tone-${item.ok ? "ok" : "bad"}`} />
                    <span>{item.label}</span>
                  </span>
                  <span className={`statusPill tone-${item.ok ? "ok" : "bad"}`}>{item.ok ? "OK" : "Review"}</span>
                </div>
              ))}
            </div>
          </Panel>
        </div>

        <Panel title="Error Preview" eyebrow="Latest error log" className="span-4">
          <LogPreview title="Error Log" items={state.errors} emptyText="No recent error logs." />
        </Panel>

        <Panel title="Heartbeat Preview" eyebrow="Latest heartbeat log" className="span-4">
          <LogPreview title="Heartbeat" items={state.heartbeats} emptyText="No heartbeat entries yet." />
        </Panel>

        <Panel title="Success Preview" eyebrow="Latest success log" className="span-4">
          <LogPreview title="Success" items={state.successes} emptyText="No success entries yet." />
        </Panel>

        <Panel title="Runtime Log Summary" eyebrow="Stored in SQLite" className="span-4">
          {state.logsSummary?.warning ? (
            <div className="alert warning compactAlert" role="alert">
              <p>{state.logsSummary.warning}</p>
            </div>
          ) : null}
          <div className="logSummaryGrid">
            <MetricCard label="Info" value={state.logsSummary?.counts?.info ?? 0} caption="Informational runtime entries" />
            <MetricCard label="Warning" value={state.logsSummary?.counts?.warning ?? 0} caption="Recoverable warnings" tone="warn" />
            <MetricCard label="Error" value={state.logsSummary?.counts?.error ?? 0} caption="Critical or failing events" tone="bad" />
          </div>
        </Panel>
      </div>
    </div>
  );
}

function RuntimePage() {
  const revisionGate = useRevisionGate();
  const [reconcileRevision, setReconcileRevision] = useState(0);
  const [overview, setOverview] = useState(null);
  const [timer, setTimer] = useState({ enabled: false, interval: 15, unit: "minutes" });
  const [actionMode, setActionMode] = useState({ mode: "notify_only" });
  const [error, setError] = useState("");
  const [saving, setSaving] = useState("");

  useDashboardReconciliation(["runtime", "settings"], () => {
    setReconcileRevision((current) => current + 1);
  });

  useEffect(() => {
    let cancelled = false;

    async function load() {
      const revision = revisionGate.issue();
      try {
        const [runtimeOverview, runtimeTimer, runtimeActionMode] = await Promise.all([
          api.getRuntimeOverview(),
          api.getRuntimeTimer(),
          api.getActionMode(),
        ]);
        if (cancelled || !revisionGate.isLatest(revision)) {
          return;
        }
        setOverview(runtimeOverview);
        setTimer({
          enabled: runtimeTimer.enabled,
          interval: runtimeTimer.interval,
          unit: runtimeTimer.unit,
        });
        setActionMode({ mode: runtimeActionMode.mode });
        setError("");
      } catch (loadError) {
        if (!cancelled && revisionGate.isLatest(revision)) {
          setError(loadError instanceof Error ? loadError.message : "Failed to load runtime overview.");
        }
      }
    }

    void load();
    const poller = window.setInterval(() => void load(), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(poller);
    };
  }, [reconcileRevision, revisionGate]);

  async function saveTimer() {
    setSaving("timer");
    try {
      const response = await api.saveRuntimeTimer(timer);
      setTimer({ enabled: response.enabled, interval: response.interval, unit: response.unit });
      setError("");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save timer.");
    } finally {
      setSaving("");
    }
  }

  async function saveActionModeValue() {
    setSaving("mode");
    try {
      const response = await api.saveActionMode(actionMode);
      setActionMode({ mode: response.mode });
      setError("");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save action mode.");
    } finally {
      setSaving("");
    }
  }

  const siteStates = Object.values(overview?.site_states ?? {});
  const selenium = overview?.selenium;
  const scanSettings = overview?.scan_settings;
  const watchlistRuntime = overview?.watchlist ?? {};
  const mediamarktWatchlistDiagnostics = watchlistRuntime.mediamarkt_diagnostics ?? {};
  const enabledSiteNames =
    (scanSettings?.effective?.enabled_sites ?? []).map((site) => SITE_LABELS[site] ?? site).join(", ") || "None";
  const seleniumConfig = selenium?.config ?? {};
  const warmTabs = selenium?.warm_tabs ?? [];
  const readyWarmTabs = warmTabs.filter((tab) => tab.warm_state === "ready").length;

  return (
    <div className="pageStack">
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Runtime internals</p>
          <h2>Track concurrency, cooldowns, queue state, and scheduled run timing.</h2>
        </div>
      </section>
      {error ? <div className="alert error">{error}</div> : null}
      <RuntimeWarnings warnings={overview?.warnings} />

      <Panel title="Per-Site State" eyebrow="Heartbeat + runtime store">
        <div className="siteCardGrid">
          {siteStates.map((site) => (
            <article className="siteCard" key={site.site}>
              <div className="siteCardHeader">
                <div>
                  <h4>{site.label}</h4>
                  <small>{site.site}</small>
                </div>
                <span className={`statusPill tone-${statusTone(site.status)}`}>{site.status}</span>
              </div>
              <dl className="siteFacts">
                <div>
                  <dt>Enabled</dt>
                  <dd>{site.enabled ? "Yes" : "No"}</dd>
                </div>
                <div>
                  <dt>Next Run</dt>
                  <dd>{formatSeconds(site.next_in_seconds)}</dd>
                </div>
                <div>
                  <dt>Cooldown</dt>
                  <dd>{formatSeconds(site.cooldown_in_seconds)}</dd>
                </div>
                <div>
                  <dt>Last Run</dt>
                  <dd>{formatDate(site.last_run_at)}</dd>
                </div>
                <div>
                  <dt>Last Success</dt>
                  <dd>{formatDate(site.last_success_at)}</dd>
                </div>
                <div>
                  <dt>Last Error</dt>
                  <dd>{formatDate(site.last_error_at)}</dd>
                </div>
                <div>
                  <dt>Items</dt>
                  <dd>{site.last_items_found}</dd>
                </div>
                <div>
                  <dt>Events</dt>
                  <dd>{site.last_events_found}</dd>
                </div>
              </dl>
              <p className="siteMessage">{site.message || site.last_error || "No site message."}</p>
            </article>
          ))}
        </div>
      </Panel>

      <div className="runtimeGrid">
        <Panel title="Priority Watchlist Runtime" eyebrow="Independent product-specific loop">
          <dl className="detailList">
            <div>
              <dt>Status</dt>
              <dd>
                {watchlistRuntime.enabled ? (watchlistRuntime.running ? "Running" : "Enabled") : "Disabled"}
              </dd>
            </div>
            <div>
              <dt>Last Cycle</dt>
              <dd>{formatDate(watchlistRuntime.last_cycle_finished_at)}</dd>
            </div>
            <div>
              <dt>Checked</dt>
              <dd>{watchlistRuntime.last_cycle_checked_count ?? 0}</dd>
            </div>
            <div>
              <dt>Changed</dt>
              <dd>{watchlistRuntime.last_cycle_changed_count ?? 0}</dd>
            </div>
            <div>
              <dt>Errors</dt>
              <dd>{watchlistRuntime.last_cycle_error_count ?? 0}</dd>
            </div>
            <div>
              <dt>Skipped</dt>
              <dd>{watchlistRuntime.last_cycle_skipped_count ?? 0}</dd>
            </div>
            <div>
              <dt>Next Cycle</dt>
              <dd>{formatSeconds(watchlistRuntime.next_cycle_in_seconds)}</dd>
            </div>
            <div>
              <dt>Enabled Items</dt>
              <dd>{watchlistRuntime.total_enabled_watchlist_items ?? 0}</dd>
            </div>
            <div>
              <dt>MediaMarkt Interval</dt>
              <dd>{formatSeconds(watchlistRuntime.intervals?.mediamarkt)}</dd>
            </div>
            <div>
              <dt>MediaMarkt Backoff</dt>
              <dd>{formatSeconds(watchlistRuntime.cooldowns?.mediamarkt)}</dd>
            </div>
            <div>
              <dt>MM Last Status</dt>
              <dd>{mediamarktWatchlistDiagnostics.last_status ?? "-"}</dd>
            </div>
            <div>
              <dt>MM Endpoint</dt>
              <dd>{mediamarktWatchlistDiagnostics.last_endpoint ?? "-"}</dd>
            </div>
            <div>
              <dt>MM HTTP</dt>
              <dd>{mediamarktWatchlistDiagnostics.last_http_status ?? "-"}</dd>
            </div>
            <div>
              <dt>MM Confidence</dt>
              <dd>{Number(mediamarktWatchlistDiagnostics.last_confidence ?? 0).toFixed(2)}</dd>
            </div>
            <div>
              <dt>MM Target</dt>
              <dd>{mediamarktWatchlistDiagnostics.last_action_target_exists ? "Ready" : "None"}</dd>
            </div>
            <div>
              <dt>MM Unknowns</dt>
              <dd>
                {mediamarktWatchlistDiagnostics.parse_unknown_count ?? 0} unknown /{" "}
                {mediamarktWatchlistDiagnostics.rate_limited_count ?? 0} limited
              </dd>
            </div>
          </dl>
          <p className="noteLine">
            {mediamarktWatchlistMessage(mediamarktWatchlistDiagnostics)}. Last skip:{" "}
            {mediamarktWatchlistDiagnostics.last_skip_reason || "-"}.
          </p>
          <p className="noteLine">
            Last error: {watchlistRuntime.last_error || "-"}
          </p>
        </Panel>

        <Panel title="Queue + Worker" eyebrow="Serialized action execution">
          <dl className="detailList">
            <div>
              <dt>Queue Size</dt>
              <dd>{overview?.queue_size ?? 0}</dd>
            </div>
            <div>
              <dt>Pending Keys</dt>
              <dd>{overview?.dispatcher?.pending?.length ?? 0}</dd>
            </div>
            <div>
              <dt>Running Keys</dt>
              <dd>{overview?.dispatcher?.running?.length ?? 0}</dd>
            </div>
            <div>
              <dt>Worker Ready</dt>
              <dd>{selenium?.ready ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Worker Busy</dt>
              <dd>{selenium?.busy ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Lifecycle</dt>
              <dd>{selenium?.lifecycle_state ?? "stopped"}</dd>
            </div>
            <div>
              <dt>Driver</dt>
              <dd>{selenium?.driver_exists ? "Exists" : "None"}</dd>
            </div>
            <div>
              <dt>Session</dt>
              <dd className="breakableText">{selenium?.driver_session_id || "-"}</dd>
            </div>
            <div>
              <dt>Browser Started</dt>
              <dd>{formatDate(selenium?.browser_started_at)}</dd>
            </div>
            <div>
              <dt>Chrome PID</dt>
              <dd>{selenium?.chrome_pid ?? "-"}</dd>
            </div>
            <div>
              <dt>ChromeDriver PID</dt>
              <dd>{selenium?.chromedriver_pid ?? "-"}</dd>
            </div>
            <div>
              <dt>Prewarmed</dt>
              <dd>{selenium?.prewarmed ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Prewarm Config</dt>
              <dd>
                {seleniumConfig.prewarm_enabled ? "enabled" : "disabled"} /{" "}
                {seleniumConfig.prewarm_on_runtime_start ? "runtime start" : "not on start"}
              </dd>
            </div>
            <div>
              <dt>Keep Alive</dt>
              <dd>{seleniumConfig.keep_browser_alive ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Warm Tabs Config</dt>
              <dd>
                {seleniumConfig.warm_tabs_enabled ? "enabled" : "disabled"} / max {seleniumConfig.warm_tabs_max ?? 0}
              </dd>
            </div>
            <div>
              <dt>Prewarm Skip</dt>
              <dd>{selenium?.last_prewarm_skip_reason || "-"}</dd>
            </div>
            <div>
              <dt>Prewarm Error</dt>
              <dd className="breakableText">{selenium?.last_prewarm_error || "-"}</dd>
            </div>
            <div>
              <dt>Active Action</dt>
              <dd className="breakableText">{selenium?.active_action || "-"}</dd>
            </div>
            <div>
              <dt>Last Action Latency</dt>
              <dd>{Number(selenium?.last_action_latency_seconds ?? 0).toFixed(2)}s</dd>
            </div>
            <div>
              <dt>First Button Search</dt>
              <dd>{Number(selenium?.last_button_search_latency_seconds ?? 0).toFixed(2)}s</dd>
            </div>
            <div>
              <dt>Duplicate Starts</dt>
              <dd>{selenium?.duplicate_start_ignored_count ?? 0}</dd>
            </div>
            <div>
              <dt>Last Result</dt>
              <dd>{selenium?.last_result || "-"}</dd>
            </div>
          </dl>
        </Panel>

        <Panel title="Selenium Warm Browser" eyebrow="Single Chrome owner + Watchlist tabs">
          <dl className="detailList">
            <div>
              <dt>Status</dt>
              <dd>{seleniumRuntimeMessage(selenium)}</dd>
            </div>
            <div>
              <dt>Prewarm</dt>
              <dd>{seleniumPrewarmMessage(selenium)}</dd>
            </div>
            <div>
              <dt>Warm Tabs</dt>
              <dd>
                {readyWarmTabs}/{selenium?.warm_tabs_max ?? 0} ready
              </dd>
            </div>
            <div>
              <dt>Enabled</dt>
              <dd>{selenium?.warm_tabs_enabled ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Action Path</dt>
              <dd>{selenium?.active_action ? "active checkout" : readyWarmTabs ? "warm tab" : "cold fallback"}</dd>
            </div>
          </dl>
          <p className="noteLine">{warmTabsMessage(selenium)}</p>
          <div className="logStack">
            {warmTabs.length ? (
              warmTabs.map((tab) => (
                <article className="logCard" key={`${tab.site}-${tab.external_id}`}>
                  <div className="logMeta">
                    <span className={`statusPill tone-${statusTone(tab.warm_state)}`}>{tab.warm_state}</span>
                    <span>{tab.site}</span>
                    <span>{tab.external_id}</span>
                  </div>
                  <p>{tab.product_title || tab.url || "-"}</p>
                  <small>
                    DOM {tab.last_dom_status || "unknown"} / age {Number(tab.age_seconds ?? 0).toFixed(1)}s / refreshed{" "}
                    {formatDate(tab.last_refreshed_at)}
                  </small>
                  {tab.last_error ? <small className="breakableText">Error: {tab.last_error}</small> : null}
                </article>
              ))
            ) : (
              <p className="emptyState">No Watchlist warm tabs are open.</p>
            )}
          </div>
        </Panel>

        <Panel title="Run Timer" eyebrow="Scheduled one-shot cycle">
          <div className="formGrid compact">
            <label className="field checkboxField">
              <span>Enable timer</span>
              <input
                checked={timer.enabled}
                onChange={(event) => setTimer((current) => ({ ...current, enabled: event.target.checked }))}
                type="checkbox"
              />
            </label>
            <label className="field">
              <span>Interval</span>
              <input
                min="1"
                onChange={(event) => setTimer((current) => ({ ...current, interval: Number(event.target.value) || 1 }))}
                type="number"
                value={timer.interval}
              />
            </label>
            <label className="field">
              <span>Unit</span>
              <select
                onChange={(event) => setTimer((current) => ({ ...current, unit: event.target.value }))}
                value={timer.unit}
              >
                <option value="seconds">Seconds</option>
                <option value="minutes">Minutes</option>
                <option value="hours">Hours</option>
              </select>
            </label>
          </div>
          <p className="noteLine">Next run: {formatDate(overview?.timer?.next_run_at)}</p>
          <p className="noteLine">Last timer run: {formatDate(overview?.timer?.last_run_at)}</p>
          <button className="button primary" onClick={() => void saveTimer()} type="button">
            {saving === "timer" ? "Saving..." : "Save Timer"}
          </button>
        </Panel>

        <Panel title="Scan Timing" eyebrow="Effective scheduler values">
          <dl className="detailList">
            <div>
              <dt>Full Scan Interval</dt>
              <dd>
                {scanSettings?.global?.scan_interval_seconds
                  ? formatSeconds(scanSettings.global.scan_interval_seconds)
                  : "Legacy per-site defaults"}
              </dd>
            </div>
            <div>
              <dt>Effective Parallel Parsers</dt>
              <dd>{scanSettings?.effective?.effective_parallel_parsers ?? scanSettings?.global?.effective_parallel_parsers ?? 0}</dd>
            </div>
            <div>
              <dt>Enabled Parsers</dt>
              <dd className="breakableText">{enabledSiteNames}</dd>
            </div>
            <div>
              <dt>Request Timeout</dt>
              <dd>{formatSeconds(scanSettings?.global?.request_timeout_seconds ?? 0)}</dd>
            </div>
            <div>
              <dt>Retry Delay</dt>
              <dd>{formatSeconds(scanSettings?.global?.retry_delay_seconds ?? 0)}</dd>
            </div>
            <div>
              <dt>Max Retries</dt>
              <dd>{scanSettings?.global?.max_retries ?? 0}</dd>
            </div>
          </dl>
          <div className="inlineStats">
            {SITE_OPTIONS.map((site) => {
              const current = scanSettings?.sites?.[site.value];
              return (
                <div className="inlineStat" key={site.value}>
                  <strong>{site.label}</strong>
                  <span>{current?.enabled ? "Enabled" : "Disabled"}</span>
                  <small>
                    Delay {formatSeconds(current?.scan_delay_seconds ?? 0)} / Cooldown {formatSeconds(current?.cooldown_seconds ?? 0)}
                  </small>
                </div>
              );
            })}
          </div>
        </Panel>

        <Panel title="Action Mode" eyebrow="Current runtime behavior">
          <div className="formGrid compact">
            <label className="field">
              <span>Action Mode</span>
              <select
                onChange={(event) => setActionMode({ mode: event.target.value })}
                value={actionMode.mode}
              >
                <option value="off">off</option>
                <option value="notify_only">notify_only</option>
                <option value="selenium">selenium</option>
              </select>
            </label>
          </div>
          <p className="noteLine">
            Notifications and parser scans can keep running without turning on Selenium checkout.
          </p>
          <button className="button primary" onClick={() => void saveActionModeValue()} type="button">
            {saving === "mode" ? "Saving..." : "Save Action Mode"}
          </button>
        </Panel>
      </div>
    </div>
  );
}

function ScanSettingsPage() {
  const revisionGate = useRevisionGate();
  const [draft, setDraft] = useState(() => buildScanSettingsDraft(null));
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState("");
  const [loading, setLoading] = useState(true);

  useDashboardReconciliation(["scan-settings", "parsers"], () => {
    if (document.activeElement?.matches?.("input, select, textarea")) {
      setNotice("Scan settings changed in another tab. Finish editing, then refresh to reconcile.");
      return;
    }
    void loadScanSettings();
  });

  useEffect(() => {
    void loadScanSettings();
  }, []);

  async function loadScanSettings() {
    const revision = revisionGate.issue();
    setLoading(true);
    try {
      const response = await api.getEffectiveScanSettings();
      if (!revisionGate.isLatest(revision)) {
        return;
      }
      setDraft(buildScanSettingsDraft(response));
      setError("");
    } catch (loadError) {
      if (revisionGate.isLatest(revision)) {
        setError(loadError instanceof Error ? loadError.message : "Failed to load scan settings.");
      }
    } finally {
      if (revisionGate.isLatest(revision)) {
        setLoading(false);
      }
    }
  }

  function updateGlobal(field, value) {
    setDraft((current) => ({
      ...current,
      global: {
        ...current.global,
        [field]: value,
      },
    }));
  }

  function updateSite(site, field, value) {
    setDraft((current) => ({
      ...current,
      sites: {
        ...current.sites,
        [site]: {
          ...current.sites[site],
          [field]: value,
        },
      },
    }));
  }

  function updateWatchlist(field, value) {
    setDraft((current) => ({
      ...current,
      watchlist: {
        ...current.watchlist,
        [field]: value,
      },
    }));
  }

  function updateWatchlistSite(site, field, value) {
    setDraft((current) => ({
      ...current,
      watchlist: {
        ...current.watchlist,
        sites: {
          ...current.watchlist.sites,
          [site]: {
            ...current.watchlist.sites[site],
            [field]: value,
          },
        },
      },
    }));
  }

  async function saveSettings() {
    setSaving("save");
    try {
      const response = await api.saveScanSettings(toScanSettingsPayload(draft));
      setDraft(buildScanSettingsDraft(response));
      setNotice("Scan settings saved. Runtime settings were refreshed on the backend.");
      setError("");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save scan settings.");
    } finally {
      setSaving("");
    }
  }

  async function resetDefaults() {
    setSaving("reset");
    try {
      const response = await api.resetScanSettings();
      setDraft(buildScanSettingsDraft(response));
      setNotice("Scan settings were reset to safe defaults.");
      setError("");
    } catch (resetError) {
      setError(resetError instanceof Error ? resetError.message : "Failed to reset scan settings.");
    } finally {
      setSaving("");
    }
  }

  return (
    <div className="pageStack">
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Real parser timing controls</p>
          <h2>Control scheduler cadence, request timing, and per-site cooldowns without moving timing logic into the frontend.</h2>
        </div>
        <div className="buttonRow">
          <button className="button ghost" onClick={() => void loadScanSettings()} type="button">
            Refresh
          </button>
          <button className="button" onClick={() => void resetDefaults()} type="button">
            {saving === "reset" ? "Resetting..." : "Reset Defaults"}
          </button>
          <button className="button primary" onClick={() => void saveSettings()} type="button">
            {saving === "save" ? "Saving..." : "Save Scan Settings"}
          </button>
        </div>
      </section>

      {error ? <div className="alert error">{error}</div> : null}
      {notice ? <div className="alert success">{notice}</div> : null}
      {loading ? <div className="panel">Loading scan settings...</div> : null}

      <Panel title="Discovery Scan Settings" eyebrow="Catalog indexer cadence">
        <div className="formGrid compact">
          <label className="field">
            <span>Full Scan Interval (seconds)</span>
            <input
              min="1"
              onChange={(event) => updateGlobal("scan_interval_seconds", event.target.value)}
              type="number"
              value={draft.global.scan_interval_seconds}
            />
          </label>
          <label className="field">
            <span>Max Parallel Parsers</span>
            <input
              max={SITE_OPTIONS.length}
              min="1"
              onChange={(event) => updateGlobal("max_parallel_parsers", event.target.value)}
              type="number"
              value={draft.global.max_parallel_parsers}
            />
          </label>
          <label className="field">
            <span>Request Timeout (seconds)</span>
            <input
              min="1"
              onChange={(event) => updateGlobal("request_timeout_seconds", event.target.value)}
              step="0.5"
              type="number"
              value={draft.global.request_timeout_seconds}
            />
          </label>
          <label className="field">
            <span>Retry Delay (seconds)</span>
            <input
              min="0"
              onChange={(event) => updateGlobal("retry_delay_seconds", event.target.value)}
              step="0.1"
              type="number"
              value={draft.global.retry_delay_seconds}
            />
          </label>
          <label className="field">
            <span>Max Retries</span>
            <input
              max="10"
              min="0"
              onChange={(event) => updateGlobal("max_retries", event.target.value)}
              type="number"
              value={draft.global.max_retries}
            />
          </label>
        </div>
        <p className="noteLine">
          Discovery Sync keeps catalog coverage broad and conservative. Priority Watchlist settings below can run faster for known matched products.
        </p>
      </Panel>

      <div className="settingsGrid">
        <Panel title="Effective Runtime Summary" eyebrow="Computed backend values">
          <dl className="detailList">
            <div>
              <dt>Enabled Sites</dt>
              <dd className="breakableText">
                {(draft.effective?.enabled_sites ?? []).map((site) => SITE_LABELS[site] ?? site).join(", ") || "None"}
              </dd>
            </div>
            <div>
              <dt>Effective Parallel Parsers</dt>
              <dd>{draft.effective?.effective_parallel_parsers ?? draft.global.effective_parallel_parsers ?? 0}</dd>
            </div>
            <div>
              <dt>Watchlist Sites</dt>
              <dd className="breakableText">
                {(draft.effective?.watchlist_enabled_sites ?? []).map((site) => SITE_LABELS[site] ?? site).join(", ") || "None"}
              </dd>
            </div>
            <div>
              <dt>Saved Storage</dt>
              <dd>{draft.storage?.exists ? "scan_settings.json present" : "Defaults only"}</dd>
            </div>
            <div>
              <dt>Storage Path</dt>
              <dd className="breakableText">{draft.storage?.path ?? "-"}</dd>
            </div>
          </dl>
        </Panel>

        <Panel title="Safety Notes" eyebrow="Keep scanning sustainable">
          <ul className="warningList">
            <li>Lower delays increase block and rate-limit risk.</li>
            <li>Higher delays are safer but reduce scan frequency.</li>
            <li>Selenium checkout and action execution stay serialized separately from parser scan concurrency.</li>
            <li>Disabled parsers stay out of scheduling even if their timing values remain saved.</li>
          </ul>
        </Panel>
      </div>

      <Panel title="Priority Watchlist Settings" eyebrow="Known matched products, separate from discovery">
        <div className="formGrid compact">
          <label className="field checkboxField">
            <span>Watchlist Enabled</span>
            <input
              checked={Boolean(draft.watchlist.enabled)}
              onChange={(event) => updateWatchlist("enabled", event.target.checked)}
              type="checkbox"
            />
          </label>
          <label className="field checkboxField">
            <span>Backoff on 429</span>
            <input
              checked={Boolean(draft.watchlist.backoff_on_429)}
              onChange={(event) => updateWatchlist("backoff_on_429", event.target.checked)}
              type="checkbox"
            />
          </label>
          <label className="field checkboxField">
            <span>Pause Site on Error</span>
            <input
              checked={Boolean(draft.watchlist.pause_site_on_error)}
              onChange={(event) => updateWatchlist("pause_site_on_error", event.target.checked)}
              type="checkbox"
            />
          </label>
          <label className="field">
            <span>Backoff Multiplier</span>
            <input
              min="1"
              onChange={(event) => updateWatchlist("backoff_multiplier", event.target.value)}
              step="0.1"
              type="number"
              value={draft.watchlist.backoff_multiplier}
            />
          </label>
          <label className="field">
            <span>Max Backoff (seconds)</span>
            <input
              min="1"
              onChange={(event) => updateWatchlist("max_backoff_seconds", event.target.value)}
              step="1"
              type="number"
              value={draft.watchlist.max_backoff_seconds}
            />
          </label>
        </div>
        <p className="noteLine">
          Watchlist checks only known products, so intervals can be lower than Discovery Sync. Low-confidence availability still will not trigger Selenium.
        </p>
        <div className="scanSiteGrid">
          {SITE_OPTIONS.map((site) => {
            const current = draft.watchlist.sites[site.value];
            return (
              <article className="scanSiteCard" key={`watchlist-${site.value}`}>
                <div className="scanSiteCardHeader">
                  <div>
                    <h4>{current.label}</h4>
                    <small>
                      Watchlist {formatSeconds(current.interval_seconds)} / Discovery {formatSeconds(current.discovery_scan_delay_seconds)}
                    </small>
                  </div>
                  <label className="checkboxRow">
                    <input
                      checked={Boolean(current.enabled)}
                      onChange={(event) => updateWatchlistSite(site.value, "enabled", event.target.checked)}
                      type="checkbox"
                    />
                    <span>Enabled</span>
                  </label>
                </div>
                <div className="formGrid compact">
                  <label className="field">
                    <span>Interval (seconds)</span>
                    <input
                      min="1"
                      onChange={(event) => updateWatchlistSite(site.value, "interval_seconds", event.target.value)}
                      step="1"
                      type="number"
                      value={current.interval_seconds}
                    />
                  </label>
                  <label className="field">
                    <span>Max Concurrency</span>
                    <input
                      min="1"
                      onChange={(event) => updateWatchlistSite(site.value, "max_concurrency", event.target.value)}
                      step="1"
                      type="number"
                      value={current.max_concurrency}
                    />
                  </label>
                  <label className="field">
                    <span>Request Timeout (seconds)</span>
                    <input
                      min="1"
                      onChange={(event) => updateWatchlistSite(site.value, "request_timeout_seconds", event.target.value)}
                      step="0.5"
                      type="number"
                      value={current.request_timeout_seconds}
                    />
                  </label>
                  <label className="field">
                    <span>Jitter (seconds)</span>
                    <input
                      min="0"
                      onChange={(event) => updateWatchlistSite(site.value, "jitter_seconds", event.target.value)}
                      step="0.1"
                      type="number"
                      value={current.jitter_seconds}
                    />
                  </label>
                </div>
              </article>
            );
          })}
        </div>
      </Panel>

      <Panel title="Discovery Per-Site Timing" eyebrow="MediaMarkt, Dreamland, Bol, and PocketGames">
        <div className="scanSiteGrid">
          {SITE_OPTIONS.map((site) => {
            const current = draft.sites[site.value];
            return (
              <article className="scanSiteCard" key={site.value}>
                <div className="scanSiteCardHeader">
                  <div>
                    <h4>{current.label}</h4>
                    <small>{site.value}</small>
                  </div>
                  <label className="checkboxRow">
                    <input
                      checked={Boolean(current.enabled)}
                      onChange={(event) => updateSite(site.value, "enabled", event.target.checked)}
                      type="checkbox"
                    />
                    <span>Enabled</span>
                  </label>
                </div>
                <div className="formGrid compact">
                  <label className="field">
                    <span>Per-site Delay (seconds)</span>
                    <input
                      min="0.1"
                      onChange={(event) => updateSite(site.value, "scan_delay_seconds", event.target.value)}
                      step="0.1"
                      type="number"
                      value={current.scan_delay_seconds}
                    />
                  </label>
                  <label className="field">
                    <span>Cooldown (seconds)</span>
                    <input
                      min="1"
                      onChange={(event) => updateSite(site.value, "cooldown_seconds", event.target.value)}
                      step="1"
                      type="number"
                      value={current.cooldown_seconds}
                    />
                  </label>
                  <label className="field">
                    <span>Request Timeout (seconds)</span>
                    <input
                      min="1"
                      onChange={(event) => updateSite(site.value, "request_timeout_seconds", event.target.value)}
                      step="0.5"
                      type="number"
                      value={current.request_timeout_seconds}
                    />
                  </label>
                  <label className="field">
                    <span>Page Delay (seconds)</span>
                    <input
                      min="0"
                      onChange={(event) => updateSite(site.value, "page_delay_seconds", event.target.value)}
                      step="0.1"
                      type="number"
                      value={current.page_delay_seconds}
                    />
                  </label>
                  <label className="field">
                    <span>Max Pages</span>
                    <input
                      disabled={!current.supports_max_pages}
                      min="1"
                      onChange={(event) => updateSite(site.value, "max_pages", event.target.value)}
                      placeholder={current.supports_max_pages ? "No cap" : "Not used"}
                      type="number"
                      value={current.max_pages ?? ""}
                    />
                  </label>
                </div>
                <p className="noteLine">
                  {current.supports_max_pages
                    ? "Max pages is optional. Leave it blank to allow the parser's normal depth."
                    : "This parser currently ignores max pages and uses its existing fetch depth."}
                </p>
              </article>
            );
          })}
        </div>
      </Panel>
    </div>
  );
}

function FiltersPage() {
  const revisionGate = useRevisionGate();
  const [filters, setFilters] = useState([]);
  const [draft, setDraft] = useState({ ...EMPTY_FILTER });
  const [selectedId, setSelectedId] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState("");

  useDashboardReconciliation(["filters"], () => {
    if (document.activeElement?.matches?.("input, select, textarea")) {
      setNotice("Filters changed in another tab. Finish editing, then refresh to reconcile.");
      return;
    }
    void loadFilters();
  });

  useEffect(() => {
    void loadFilters();
  }, []);

  async function loadFilters() {
    const revision = revisionGate.issue();
    try {
      const response = await api.getFilters();
      if (!revisionGate.isLatest(revision)) {
        return;
      }
      const nextFilters = Array.isArray(response) ? response : response.items ?? [];
      setFilters(nextFilters);

      const selectedItem = nextFilters.find((item) => item.id === selectedId);
      if (selectedItem) {
        setDraft(filterToDraft(selectedItem));
      } else if (selectedId != null) {
        beginCreate();
      }
      setError("");
    } catch (loadError) {
      if (revisionGate.isLatest(revision)) {
        setError(loadError instanceof Error ? loadError.message : "Failed to load filters.");
      }
    }
  }

  function beginCreate() {
    setSelectedId(null);
    setDraft({ ...EMPTY_FILTER });
  }

  function selectFilter(item) {
    setSelectedId(item.id);
    setDraft(filterToDraft(item));
  }

  async function saveFilter() {
    setSaving("save");
    try {
      const payload = draftToPayload(draft);
      if (selectedId == null) {
        await api.createFilter(payload);
      } else {
        await api.updateFilter(selectedId, payload);
      }
      await loadFilters();
      setNotice(selectedId == null ? "Filter created." : "Filter saved.");
      beginCreate();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save filter.");
    } finally {
      setSaving("");
    }
  }

  async function deleteFilter() {
    if (selectedId == null) {
      return;
    }
    setSaving("delete");
    try {
      await api.deleteFilter(selectedId);
      await loadFilters();
      setNotice("Filter deleted.");
      beginCreate();
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : "Failed to delete filter.");
    } finally {
      setSaving("");
    }
  }

  async function toggleFilter(id) {
    setSaving(`toggle-${id}`);
    try {
      await api.toggleFilter(id);
      await loadFilters();
      setNotice("Filter status updated.");
    } catch (toggleError) {
      setError(toggleError instanceof Error ? toggleError.message : "Failed to toggle filter.");
    } finally {
      setSaving("");
    }
  }

  return (
    <div className="pageStack">
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Saved filters and editor</p>
          <h2>Create, edit, and toggle filter rules across all four sites.</h2>
        </div>
        <div className="buttonRow">
          <button className="button ghost" onClick={beginCreate} type="button">
            New Filter
          </button>
        </div>
      </section>
      {error ? <div className="alert error">{error}</div> : null}
      {notice ? <div className="alert success">{notice}</div> : null}

      <div className="filtersLayout">
        <Panel title="Saved Filters" eyebrow="Current rule list">
          {filters.length ? (
            <div className="filterList">
              {filters.map((item) => (
                <article
                  key={item.id}
                  className={`filterCard ${selectedId === item.id ? "is-active" : ""}`}
                  onClick={() => selectFilter(item)}
                >
                  <div className="filterMeta">
                    <div>
                      <h4>{item.name || `Filter #${item.id}`}</h4>
                      <small>{formatSiteList(item.sites)}</small>
                    </div>
                    <button
                      className={`toggleButton ${item.enabled ? "is-enabled" : ""}`}
                      onClick={(event) => {
                        event.stopPropagation();
                        void toggleFilter(item.id);
                      }}
                      type="button"
                    >
                      {saving === `toggle-${item.id}` ? "..." : item.enabled ? "On" : "Off"}
                    </button>
                  </div>
                  <p>{(item.keyword_groups ?? []).map((group) => group.join(", ")).join(" | ") || "No keywords yet"}</p>
                </article>
              ))}
            </div>
          ) : (
            <div className="emptyStateCard">
              <p className="emptyState">No saved filters yet.</p>
              <p className="noteLine">Create your first filter on the right to populate this list.</p>
              <button className="button" onClick={beginCreate} type="button">
                Create Filter
              </button>
            </div>
          )}
        </Panel>

        <Panel title={selectedId == null ? "Create Filter" : `Edit Filter #${selectedId}`} eyebrow="Dreamland stays available in the site selector">
          <div className="formGrid">
            <label className="field">
              <span>Name</span>
              <input
                onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))}
                value={draft.name}
              />
            </label>
            <div className="field">
              <span>Sites</span>
              <div className="checkGrid siteCheckGrid">
                {SITE_OPTIONS.map((site) => (
                  <label className="checkboxRow" key={site.value}>
                    <input
                      checked={draft.sites.includes(site.value)}
                      onChange={(event) =>
                        setDraft((current) => ({
                          ...current,
                          sites: event.target.checked
                            ? [...current.sites, site.value]
                            : current.sites.filter((entry) => entry !== site.value),
                        }))
                      }
                      type="checkbox"
                    />
                    <span>{site.label}</span>
                  </label>
                ))}
              </div>
            </div>
            <label className="field span-full">
              <span>Keyword groups</span>
              <textarea
                onChange={(event) => setDraft((current) => ({ ...current, keywordGroupsText: event.target.value }))}
                rows="6"
                value={draft.keywordGroupsText}
              />
              <small>One line per group. Separate alternative words with commas.</small>
            </label>
            <label className="field span-full">
              <span>Exclude words</span>
              <input
                onChange={(event) => setDraft((current) => ({ ...current, excludeWordsText: event.target.value }))}
                value={draft.excludeWordsText}
              />
            </label>
            <label className="field">
              <span>Min price</span>
              <input
                onChange={(event) => setDraft((current) => ({ ...current, minPrice: event.target.value }))}
                type="number"
                value={draft.minPrice}
              />
            </label>
            <label className="field">
              <span>Max price</span>
              <input
                onChange={(event) => setDraft((current) => ({ ...current, maxPrice: event.target.value }))}
                type="number"
                value={draft.maxPrice}
              />
            </label>
            <label className="field checkboxField">
              <span>Soft price matching</span>
              <input
                checked={draft.softPrice}
                onChange={(event) => setDraft((current) => ({ ...current, softPrice: event.target.checked }))}
                type="checkbox"
              />
            </label>
            <label className="field checkboxField">
              <span>Enabled</span>
              <input
                checked={draft.enabled}
                onChange={(event) => setDraft((current) => ({ ...current, enabled: event.target.checked }))}
                type="checkbox"
              />
            </label>
          </div>
          <div className="buttonRow">
            <button className="button primary" onClick={() => void saveFilter()} type="button">
              {saving === "save" ? "Saving..." : selectedId == null ? "Create Filter" : "Save Filter"}
            </button>
            <button className="button" onClick={beginCreate} type="button">
              Reset
            </button>
            <button
              className="button danger"
              disabled={selectedId == null}
              onClick={() => void deleteFilter()}
              type="button"
            >
              {saving === "delete" ? "Deleting..." : "Delete"}
            </button>
          </div>
        </Panel>
      </div>
    </div>
  );
}

function WatchlistPage() {
  const revisionGate = useRevisionGate();
  const [items, setItems] = useState([]);
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [manual, setManual] = useState({
    site: "mediamarkt",
    product_key: "",
    article_number: "",
    sku: "",
    handle: "",
    title: "",
    url: "",
    pinned: true,
  });

  useDashboardReconciliation(["watchlist", "filters"], () => void load());

  async function load() {
    const revision = revisionGate.issue();
    try {
      const response = await api.getWatchlistState();
      if (!revisionGate.isLatest(revision)) {
        return;
      }
      setItems(response.items ?? []);
      setSummary(response.summary ?? null);
      setError("");
    } catch (loadError) {
      if (revisionGate.isLatest(revision)) {
        setError(loadError instanceof Error ? loadError.message : "Failed to load Watchlist.");
      }
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function runAction(action, callback) {
    setBusy(action);
    try {
      await callback();
      await load();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Watchlist action failed.");
    } finally {
      setBusy("");
    }
  }

  async function addManual() {
    await api.addManualWatchlistItem({
      ...manual,
      product_key: manual.product_key || manual.sku || manual.article_number || manual.handle,
    });
    setManual({
      site: "mediamarkt",
      product_key: "",
      article_number: "",
      sku: "",
      handle: "",
      title: "",
      url: "",
      pinned: true,
    });
  }

  const siteCounts = summary?.sites ?? {};

  return (
    <div className="pageStack">
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Priority Watchlist</p>
          <h2>Track matched products separately from catalog discovery.</h2>
        </div>
        <div className="heroActions">
          <button className="button ghost" type="button" onClick={() => void runAction("refresh", load)}>
            Refresh
          </button>
          <button className="button" type="button" disabled={Boolean(busy)} onClick={() => void runAction("build", api.buildWatchlist)}>
            {busy === "build" ? "Building..." : "Build From Filters"}
          </button>
          <button className="button primary" type="button" disabled={Boolean(busy)} onClick={() => void runAction("sync", api.syncWatchlist)}>
            {busy === "sync" ? "Syncing..." : "Force Sync All"}
          </button>
        </div>
      </section>

      {error ? <div className="alert error">{error}</div> : null}

      <section className="metricGrid">
        <MetricCard label="Total" value={summary?.total ?? 0} caption="Watchlist rows" />
        <MetricCard label="Enabled" value={summary?.enabled ?? 0} caption="Active checks" tone="live" />
        <MetricCard label="Available" value={summary?.available ?? 0} caption="High-confidence" tone="live" />
        <MetricCard label="Unavailable" value={summary?.out_of_stock ?? 0} caption="Out, notify-only, not found" tone="warn" />
        <MetricCard label="Unknown" value={summary?.unknown_or_error ?? 0} caption="Needs inspection" tone="bad" />
        <MetricCard label="Orphaned" value={summary?.orphaned ?? 0} caption="No enabled filter" tone="warn" />
      </section>

      <section className="dashboardGrid">
        <Panel title="Watchlist Items" eyebrow="Product-specific sync" className="span-12">
          <div className="table">
            <div className="tableRow tableHead">
              <span>Product</span>
              <span>Channel</span>
              <span>Status</span>
              <span>Confidence</span>
              <span>Filters</span>
              <span>Timeline</span>
              <span>Actions</span>
            </div>
            {items.length ? (
              items.map((item) => {
                const diagnostics = item.site === "mediamarkt" ? watchlistItemDiagnostics(item) : null;
                return (
                <div className="tableRow" key={item.id}>
                  <span>
                    <strong>{item.title || item.product_key}</strong>
                    <small>{item.product_key}</small>
                    {item.url ? (
                      <a href={item.url} target="_blank" rel="noreferrer">
                        Open product
                      </a>
                    ) : null}
                  </span>
                  <span>
                    <strong>{SITE_LABELS[item.site] ?? item.site}</strong>
                    <small>{item.source}</small>
                  </span>
                  <span>
                    <span className={`statusPill tone-${statusTone(item.current_inventory_status)}`}>
                      {item.current_inventory_status}
                    </span>
                    {item.last_error ? <small>{item.last_error}</small> : null}
                    {diagnostics ? <small>{mediamarktWatchlistMessage(diagnostics)}</small> : null}
                  </span>
                  <span>
                    {Number(item.status_confidence_score ?? 0).toFixed(2)}
                    {diagnostics ? (
                      <small>
                        {diagnostics.last_endpoint || "-"} / HTTP {diagnostics.last_http_status ?? "-"}
                      </small>
                    ) : null}
                    {diagnostics ? (
                      <small>
                        Target {diagnostics.last_action_target_exists ? "ready" : "none"} /{" "}
                        {diagnostics.buyable_marker_found ? "buyable marker" : "no buyable marker"}
                      </small>
                    ) : null}
                  </span>
                  <span>{(item.matched_filter_names ?? []).join(", ") || "-"}</span>
                  <span>
                    <small>Checked {formatDate(item.last_checked_at)}</small>
                    <small>Seen {formatDate(item.last_seen_at)}</small>
                    <small>Changed {formatDate(item.last_status_change_at)}</small>
                    {diagnostics?.last_skip_reason ? <small>Skip {diagnostics.last_skip_reason}</small> : null}
                  </span>
                  <span className="buttonRow">
                    <button className="button ghost" type="button" onClick={() => void runAction(`sync-${item.id}`, () => api.syncWatchlistItem(item.id))}>
                      Sync
                    </button>
                    <button className="button ghost" type="button" onClick={() => void runAction(`pin-${item.id}`, () => api.patchWatchlistItem(item.id, { pinned: !item.pinned }))}>
                      {item.pinned ? "Unpin" : "Pin"}
                    </button>
                    <button className="button ghost" type="button" onClick={() => void runAction(`enable-${item.id}`, () => api.patchWatchlistItem(item.id, { enabled: !item.enabled }))}>
                      {item.enabled ? "Disable" : "Enable"}
                    </button>
                    <button className="button danger" type="button" onClick={() => void runAction(`delete-${item.id}`, () => api.deleteWatchlistItem(item.id))}>
                      Remove
                    </button>
                  </span>
                </div>
                );
              })
            ) : (
              <div className="emptyState">No Watchlist items yet.</div>
            )}
          </div>
        </Panel>

        <Panel title="Add Manual Product" eyebrow="URL or SKU" className="span-8">
          <div className="formGrid compact">
            <label className="field">
              <span>Channel</span>
              <select value={manual.site} onChange={(event) => setManual((current) => ({ ...current, site: event.target.value }))}>
                {SITE_OPTIONS.map((site) => (
                  <option key={site.value} value={site.value}>
                    {site.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Product key</span>
              <input value={manual.product_key} onChange={(event) => setManual((current) => ({ ...current, product_key: event.target.value }))} />
            </label>
            <label className="field">
              <span>SKU / Article</span>
              <input value={manual.sku} onChange={(event) => setManual((current) => ({ ...current, sku: event.target.value, article_number: event.target.value }))} />
            </label>
            <label className="field">
              <span>Handle</span>
              <input value={manual.handle} onChange={(event) => setManual((current) => ({ ...current, handle: event.target.value }))} />
            </label>
            <label className="field">
              <span>Title</span>
              <input value={manual.title} onChange={(event) => setManual((current) => ({ ...current, title: event.target.value }))} />
            </label>
            <label className="field">
              <span>URL</span>
              <input value={manual.url} onChange={(event) => setManual((current) => ({ ...current, url: event.target.value }))} />
            </label>
            <label className="checkboxRow">
              <input type="checkbox" checked={manual.pinned} onChange={(event) => setManual((current) => ({ ...current, pinned: event.target.checked }))} />
              <span>Pinned</span>
            </label>
          </div>
          <div className="buttonRow">
            <button className="button primary" type="button" disabled={!manual.url || (!manual.product_key && !manual.sku && !manual.handle)} onClick={() => void runAction("manual", addManual)}>
              Add Manual
            </button>
          </div>
        </Panel>

        <Panel title="Per-Channel Counts" eyebrow="Current Watchlist" className="span-4">
          <dl className="detailList">
            {SITE_OPTIONS.map((site) => {
              const counts = siteCounts[site.value] ?? {};
              return (
                <div key={site.value}>
                  <dt>{site.label}</dt>
                  <dd>
                    {counts.total ?? 0} total / {counts.enabled ?? 0} enabled / {counts.available ?? 0} available
                  </dd>
                </div>
              );
            })}
          </dl>
        </Panel>
      </section>
    </div>
  );
}

function LogsPage() {
  const revisionGate = useRevisionGate();
  const [reconcileRevision, setReconcileRevision] = useState(0);
  const [type, setType] = useState("error");
  const [site, setSite] = useState("");
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const [items, setItems] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useDashboardReconciliation(["logs", "runtime", "watchlist"], () => {
    setReconcileRevision((current) => current + 1);
  });

  useEffect(() => {
    let cancelled = false;

    async function load() {
      const revision = revisionGate.issue();
      setBusy(true);
      try {
        const response = await api.getLogs({ type, site, q: deferredQuery, limit: 250 });
        if (!cancelled && revisionGate.isLatest(revision)) {
          setItems(response.items ?? []);
          setError("");
        }
      } catch (loadError) {
        if (!cancelled && revisionGate.isLatest(revision)) {
          setError(loadError instanceof Error ? loadError.message : "Failed to load logs.");
        }
      } finally {
        if (!cancelled && revisionGate.isLatest(revision)) {
          setBusy(false);
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [type, site, deferredQuery, reconcileRevision, revisionGate]);

  return (
    <div className="pageStack">
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Structured runtime logs</p>
          <h2>Filter by type, search text, and isolate one site without losing the shared timeline.</h2>
        </div>
      </section>
      {error ? <div className="alert error">{error}</div> : null}

      <Panel title="Filters" eyebrow="Logs API controls">
        <div className="formGrid compact">
          <label className="field">
            <span>Type</span>
            <select onChange={(event) => setType(event.target.value)} value={type}>
              <option value="error">Error</option>
              <option value="heartbeat">Heartbeat</option>
              <option value="success">Success</option>
              <option value="worker_trace">Worker Trace</option>
              <option value="">All</option>
            </select>
          </label>
          <label className="field">
            <span>Site</span>
            <select onChange={(event) => setSite(event.target.value)} value={site}>
              <option value="">All sites</option>
              {SITE_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field span-2">
            <span>Search</span>
            <input onChange={(event) => setQuery(event.target.value)} placeholder="external_id, cooldown, deny, heartbeat..." value={query} />
          </label>
        </div>
      </Panel>

      <Panel
        title="Log Timeline"
        eyebrow={busy ? "Refreshing..." : `${items.length} items loaded`}
        action={
          <button className="button ghost" onClick={() => setQuery((current) => `${current}`)} type="button">
            Refresh
          </button>
        }
      >
        <div className="logStack">
          {items.length ? (
            items.map((item) => (
              <article className="timelineItem" key={`${item.id ?? item.timestamp}-${item.message}`}>
                <div className="timelineRail">
                  <span className={`statusDot tone-${statusTone(item.category || item.level?.toLowerCase())}`} />
                </div>
                <div className="timelineBody">
                  <div className="timelineMeta">
                    <span className={`statusPill tone-${statusTone(item.category || item.level?.toLowerCase())}`}>
                      {item.category || item.level}
                    </span>
                    <span>{item.site || "system"}</span>
                    <span>{formatDate(item.timestamp)}</span>
                  </div>
                  <p>{item.message}</p>
                  {item.details_json ? <small>{item.details_json}</small> : null}
                </div>
              </article>
            ))
          ) : (
            <p className="emptyState">No logs match the current filters.</p>
          )}
        </div>
      </Panel>
    </div>
  );
}

function SettingsPage() {
  const revisionGate = useRevisionGate();
  const [credentials, setCredentials] = useState([]);
  const [credentialValues, setCredentialValues] = useState({});
  const [proxy, setProxy] = useState({ enabled: false, type: "http", host: "", port: 0, login: "", password: "" });
  const [telegram, setTelegram] = useState({ bot_token: "", chat_id: "" });
  const [notifications, setNotifications] = useState({
    enabled: true,
    heartbeat_alerts: true,
    success_alerts: true,
    error_alerts: true,
    worker_trace_enabled: true,
    worker_trace_level: "normal",
    worker_trace_queue_update_seconds: 60,
  });
  const [timer, setTimer] = useState({ enabled: false, interval: 15, unit: "minutes" });
  const [actionMode, setActionMode] = useState({ mode: "notify_only" });
  const [workerSettings, setWorkerSettings] = useState({
    queue_check_enabled: true,
    queue_wait_timeout_seconds: 300,
    queue_poll_seconds: 1,
    worker_speed_profile: "balanced",
    worker_click_pause_seconds: 0.2,
    worker_after_navigation_wait_seconds: 0.5,
    worker_after_add_to_cart_wait_seconds: 0.6,
    worker_after_checkout_click_wait_seconds: 0.6,
    worker_wait_timeout_seconds: 20,
    worker_poll_seconds: 0.2,
    worker_retry_pause_seconds: 0.45,
  });
  const [dbStatus, setDbStatus] = useState(null);
  const [configStatus, setConfigStatus] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState("");
  const [showSensitive, setShowSensitive] = useState(false);
  const [chromeProfileInfo, setChromeProfileInfo] = useState(null);
  const [backupPayload, setBackupPayload] = useState(null);
  const [backupFileName, setBackupFileName] = useState("");
  const [restorePreview, setRestorePreview] = useState(null);
  const [snapshotInfo, setSnapshotInfo] = useState(null);

  useDashboardReconciliation(
    ["settings", "credentials", "proxy", "telegram", "runtime", "parsers"],
    () => {
      if (document.activeElement?.matches?.("input, select, textarea")) {
        setNotice("Settings changed in another tab. Finish editing, then reload to reconcile.");
        return;
      }
      void loadAll();
    },
  );

  useEffect(() => {
    void loadAll();
  }, []);

  async function loadAll() {
    const revision = revisionGate.issue();
    try {
      const [
        credentialsResponse,
        proxyResponse,
        telegramResponse,
        notificationsResponse,
        workerSettingsResponse,
        timerResponse,
        actionModeResponse,
        dbResponse,
        configResponse,
      ] = await Promise.all([
        api.getCredentials(),
        api.getProxy(),
        api.getTelegram(),
        api.getNotifications(),
        api.getWorkerSettings(),
        api.getRuntimeTimer(),
        api.getActionMode(),
        api.getDbStatus(),
        api.getConfigStatus(),
      ]);

      if (!revisionGate.isLatest(revision)) {
        return;
      }

      setCredentials(credentialsResponse.items ?? []);
      setCredentialValues(
        Object.fromEntries((credentialsResponse.items ?? []).map((item) => [item.key, item.value])),
      );
      setProxy(proxyResponse);
      setTelegram(telegramResponse);
      setNotifications(notificationsResponse);
      setWorkerSettings(workerSettingsResponse);
      setTimer({ enabled: timerResponse.enabled, interval: timerResponse.interval, unit: timerResponse.unit });
      setActionMode({ mode: actionModeResponse.mode });
      setDbStatus(dbResponse);
      setConfigStatus(configResponse);
      setError("");
    } catch (loadError) {
      if (revisionGate.isLatest(revision)) {
        setError(loadError instanceof Error ? loadError.message : "Failed to load settings.");
      }
    }
  }

  async function saveCredentialsPanel() {
    setSaving("credentials");
    try {
      await api.saveCredentials(credentialValues);
      await loadAll();
      setNotice("Credentials saved.");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save credentials.");
    } finally {
      setSaving("");
    }
  }

  async function saveProxyPanel() {
    setSaving("proxy");
    try {
      await api.saveProxy(proxy);
      await loadAll();
      setNotice("Proxy settings saved.");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save proxy settings.");
    } finally {
      setSaving("");
    }
  }

  async function saveTelegramPanel() {
    setSaving("telegram");
    try {
      await api.saveTelegram(telegram);
      await loadAll();
      setNotice("Telegram settings saved.");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save Telegram settings.");
    } finally {
      setSaving("");
    }
  }

  async function testTelegramPanel() {
    setSaving("telegram-test");
    try {
      await api.testTelegram({ text: "Dashboard Telegram test" });
      setError("");
      setNotice("Telegram test sent.");
    } catch (testError) {
      setError(testError instanceof Error ? testError.message : "Failed to send Telegram test.");
    } finally {
      setSaving("");
    }
  }

  async function testProxyPanel() {
    setSaving("proxy-test");
    try {
      await api.testProxy(proxy);
      setError("");
      setNotice("Proxy test completed.");
    } catch (testError) {
      setError(testError instanceof Error ? testError.message : "Failed to test proxy.");
    } finally {
      setSaving("");
    }
  }

  async function saveNotificationsPanel() {
    setSaving("notifications");
    try {
      await api.saveNotifications(notifications);
      await loadAll();
      setNotice("Notification settings saved.");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save notifications.");
    } finally {
      setSaving("");
    }
  }

  async function saveRuntimePanels() {
    setSaving("runtime");
    try {
      await Promise.all([api.saveRuntimeTimer(timer), api.saveActionMode(actionMode), api.saveWorkerSettings(workerSettings)]);
      await loadAll();
      setNotice("Runtime settings saved.");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save runtime settings.");
    } finally {
      setSaving("");
    }
  }

  async function backupDatabase() {
    setSaving("backup");
    try {
      await api.backupDb();
      await loadAll();
      setNotice("Database backup created.");
    } catch (backupError) {
      setError(backupError instanceof Error ? backupError.message : "Failed to backup database.");
    } finally {
      setSaving("");
    }
  }

  async function clearOldLogs() {
    setSaving("clear-logs");
    try {
      await api.clearOldLogs(30);
      await loadAll();
      setNotice("Old logs cleared.");
    } catch (cleanupError) {
      setError(cleanupError instanceof Error ? cleanupError.message : "Failed to clear old logs.");
    } finally {
      setSaving("");
    }
  }

  async function clearStaleActions() {
    setSaving("clear-actions");
    try {
      await api.clearStaleActions(30);
      await loadAll();
      setNotice("Stale actions cleared.");
    } catch (cleanupError) {
      setError(cleanupError instanceof Error ? cleanupError.message : "Failed to clear stale actions.");
    } finally {
      setSaving("");
    }
  }

  async function downloadSettingsBackup() {
    setSaving("settings-download");
    try {
      const response = await api.exportSettingsBackup();
      saveBlobAsFile(response.blob, response.filename);
      setError("");
      setNotice("Settings backup prepared.");
    } catch (downloadError) {
      setError(downloadError instanceof Error ? downloadError.message : "Failed to download settings backup.");
    } finally {
      setSaving("");
    }
  }

  async function selectBackupFile(event) {
    const file = event.target.files?.[0];
    setBackupFileName(file?.name || "");
    setBackupPayload(null);
    setRestorePreview(null);
    if (!file) {
      return;
    }
    try {
      const parsed = JSON.parse(await file.text());
      setBackupPayload(parsed);
      setError("");
      setNotice("Backup file loaded. Preview it before applying.");
    } catch (parseError) {
      setError(parseError instanceof Error ? parseError.message : "Backup file must be valid JSON.");
    }
  }

  async function previewSettingsRestore() {
    if (!backupPayload) {
      return;
    }
    setSaving("settings-preview");
    try {
      const response = await api.previewSettingsRestore(backupPayload);
      setRestorePreview(response);
      setError("");
      setNotice(response.will_change ? "Restore preview is ready." : "Backup already matches current settings.");
    } catch (previewError) {
      setRestorePreview(null);
      setError(previewError instanceof Error ? previewError.message : "Failed to preview settings restore.");
    } finally {
      setSaving("");
    }
  }

  async function applySettingsRestore() {
    if (!backupPayload || !restorePreview?.valid) {
      return;
    }
    if (!window.confirm("Apply this settings restore? A local snapshot will be created first.")) {
      return;
    }
    setSaving("settings-restore");
    try {
      const response = await api.restoreSettingsBackup(backupPayload);
      await loadAll();
      setSnapshotInfo(response.pre_restore_snapshot ?? null);
      setRestorePreview(null);
      setError("");
      setNotice("Settings restore applied. A pre-restore snapshot was saved locally.");
    } catch (restoreError) {
      setError(restoreError instanceof Error ? restoreError.message : "Failed to apply settings restore.");
    } finally {
      setSaving("");
    }
  }

  async function saveLocalSettingsSnapshot() {
    setSaving("settings-snapshot");
    try {
      const response = await api.saveSettingsSnapshot();
      setSnapshotInfo(response);
      setError("");
      setNotice("Local settings snapshot saved.");
    } catch (snapshotError) {
      setError(snapshotError instanceof Error ? snapshotError.message : "Failed to save local settings snapshot.");
    } finally {
      setSaving("");
    }
  }

  async function launchChromeProfile() {
    setSaving("chrome-profile");
    try {
      const response = await api.createChromeProfile();
      setChromeProfileInfo(response);
      setNotice(response.message || "Chrome profile launched.");
      setError("");
    } catch (launchError) {
      setError(launchError instanceof Error ? launchError.message : "Failed to create Chrome profile.");
    } finally {
      setSaving("");
    }
  }

  const groupedCredentials = useMemo(() => groupCredentials(credentials), [credentials]);
  const backupFilterCount =
    restorePreview?.backup_summary?.filters_count ?? (Array.isArray(backupPayload?.filters) ? backupPayload.filters.length : 0);
  const backupWatchlistCount =
    restorePreview?.backup_summary?.watchlist_items_count ??
    (Array.isArray(backupPayload?.watchlist_items) ? backupPayload.watchlist_items.length : 0);
  const previewGroups = restorePreview?.groups ?? [];
  const changedPreviewGroups = previewGroups.filter((group) => group.will_change);

  return (
    <div className="pageStack">
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Settings and owner tools</p>
          <h2>Persist real config, test integrations, and keep the system manageable without living in the console.</h2>
        </div>
      </section>
      {error ? <div className="alert error">{error}</div> : null}
      {notice ? <div className="alert success">{notice}</div> : null}

      <div className="settingsGrid">
        <Panel title="Config Status" eyebrow="Validation and env review">
          <div className="validationList">
            {(configStatus?.checks ?? []).map((item) => (
              <div className="validationRow" key={item.key}>
                <span className="validationLabel">
                  <span className={`statusDot tone-${item.ok ? "ok" : "bad"}`} />
                  <span>{item.label}</span>
                </span>
                <span className={`statusPill tone-${item.ok ? "ok" : "bad"}`}>{item.ok ? "OK" : "Review"}</span>
              </div>
            ))}
          </div>
        </Panel>

        <Panel title="Chrome Profile" eyebrow="Launch configured browser profile">
          <p className="panelText">
            Uses <code>CHROME_BINARY</code>, <code>CHROME_USER_DATA_DIR</code>, and <code>CHROME_PROFILE_DIR</code> when
            they are available. If Chrome is installed in a normal Windows location, the backend can fall back to that path.
          </p>
          {chromeProfileInfo ? (
            <dl className="detailList">
              <div>
                <dt>Chrome Binary</dt>
                <dd className="breakableText">{chromeProfileInfo.chrome_binary}</dd>
              </div>
              <div>
                <dt>User Data Dir</dt>
                <dd className="breakableText">{chromeProfileInfo.chrome_user_data_dir}</dd>
              </div>
              <div>
                <dt>Profile Dir</dt>
                <dd className="breakableText">{chromeProfileInfo.chrome_profile_dir}</dd>
              </div>
              <div>
                <dt>Process Id</dt>
                <dd>{chromeProfileInfo.pid ?? "-"}</dd>
              </div>
            </dl>
          ) : null}
          <button className="button primary" onClick={() => void launchChromeProfile()} type="button">
            {saving === "chrome-profile" ? "Launching..." : "Create Chrome Profile"}
          </button>
        </Panel>

        <Panel title="Settings Backup" eyebrow="Safe export and restore">
          <div className="alert warning">
            <p>
              Backups include normal preferences, filters, parser flags, and Watchlist configuration. Private
              account, browser session, payment, token, and password data is not included.
            </p>
          </div>
          <div className="buttonRow settingsBackupActions">
            <button className="button primary" onClick={() => void downloadSettingsBackup()} type="button">
              {saving === "settings-download" ? "Preparing..." : "Download Settings Backup"}
            </button>
            <label className="button fileButton">
              <input accept="application/json,.json" onChange={(event) => void selectBackupFile(event)} type="file" />
              <span>Select Backup File</span>
            </label>
            <button
              className="button"
              disabled={!backupPayload || saving === "settings-preview"}
              onClick={() => void previewSettingsRestore()}
              type="button"
            >
              {saving === "settings-preview" ? "Previewing..." : "Preview Restore"}
            </button>
            <button
              className="button danger"
              disabled={!backupPayload || !restorePreview?.valid || saving === "settings-restore"}
              onClick={() => void applySettingsRestore()}
              type="button"
            >
              {saving === "settings-restore" ? "Applying..." : "Apply Restore"}
            </button>
            <button className="button" onClick={() => void saveLocalSettingsSnapshot()} type="button">
              {saving === "settings-snapshot" ? "Saving..." : "Save Local Snapshot"}
            </button>
          </div>

          {backupFileName ? <p className="selectedFileName">Selected: {backupFileName}</p> : null}

          <div className="inlineStats settingsBackupStats">
            <div className="inlineStat">
              <span>Filters in backup</span>
              <strong>{backupFilterCount}</strong>
            </div>
            <div className="inlineStat">
              <span>Watchlist entries</span>
              <strong>{backupWatchlistCount}</strong>
            </div>
          </div>

          {restorePreview ? (
            <div className="restorePreview">
              <div className="subHeader">
                <div>
                  <h4>{changedPreviewGroups.length ? "Settings groups that will change" : "No changes detected"}</h4>
                  <p>{restorePreview.exported_at ? `Exported ${formatDate(restorePreview.exported_at)}` : "Backup timestamp unavailable"}</p>
                </div>
              </div>
              <ul className="statusRows">
                {previewGroups.map((group) => (
                  <li className="statusRow" key={group.key}>
                    <span>{group.label}</span>
                    <span className={`statusPill tone-${group.will_change ? "warn" : "ok"}`}>
                      {group.summary}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="emptyState">Select a JSON backup and preview it before applying changes.</p>
          )}

          {snapshotInfo?.path ? (
            <dl className="detailList snapshotDetails">
              <div>
                <dt>Latest snapshot</dt>
                <dd className="breakableText">{snapshotInfo.path}</dd>
              </div>
            </dl>
          ) : null}
        </Panel>

        <Panel title="Telegram" eyebrow="Token, chat id, test message">
          <div className="formGrid compact">
            <label className="field">
              <span>Bot token</span>
              <input
                onChange={(event) => setTelegram((current) => ({ ...current, bot_token: event.target.value }))}
                type={showSensitive ? "text" : "password"}
                value={telegram.bot_token ?? ""}
              />
            </label>
            <label className="field">
              <span>Chat id</span>
              <input
                onChange={(event) => setTelegram((current) => ({ ...current, chat_id: event.target.value }))}
                value={telegram.chat_id ?? ""}
              />
            </label>
          </div>
          <div className="buttonRow">
            <button className="button primary" onClick={() => void saveTelegramPanel()} type="button">
              {saving === "telegram" ? "Saving..." : "Save Telegram"}
            </button>
            <button className="button" onClick={() => void testTelegramPanel()} type="button">
              {saving === "telegram-test" ? "Sending..." : "Send Test"}
            </button>
          </div>
        </Panel>

        <Panel title="Proxy" eyebrow="Global scan/fetch proxy">
          <div className="formGrid compact">
            <label className="field checkboxField">
              <span>Enabled</span>
              <input
                checked={proxy.enabled ?? false}
                onChange={(event) => setProxy((current) => ({ ...current, enabled: event.target.checked }))}
                type="checkbox"
              />
            </label>
            <label className="field">
              <span>Type</span>
              <select
                onChange={(event) => setProxy((current) => ({ ...current, type: event.target.value }))}
                value={proxy.type ?? "http"}
              >
                <option value="http">http</option>
                <option value="https">https</option>
                <option value="socks5">socks5</option>
              </select>
            </label>
            <label className="field">
              <span>Host</span>
              <input
                onChange={(event) => setProxy((current) => ({ ...current, host: event.target.value }))}
                value={proxy.host ?? ""}
              />
            </label>
            <label className="field">
              <span>Port</span>
              <input
                onChange={(event) => setProxy((current) => ({ ...current, port: Number(event.target.value) || 0 }))}
                type="number"
                value={proxy.port ?? 0}
              />
            </label>
            <label className="field">
              <span>Login</span>
              <input
                onChange={(event) => setProxy((current) => ({ ...current, login: event.target.value }))}
                value={proxy.login ?? ""}
              />
            </label>
            <label className="field">
              <span>Password</span>
              <input
                onChange={(event) => setProxy((current) => ({ ...current, password: event.target.value }))}
                type={showSensitive ? "text" : "password"}
                value={proxy.password ?? ""}
              />
            </label>
          </div>
          <div className="buttonRow">
            <button className="button primary" onClick={() => void saveProxyPanel()} type="button">
              {saving === "proxy" ? "Saving..." : "Save Proxy"}
            </button>
            <button className="button" onClick={() => void testProxyPanel()} type="button">
              {saving === "proxy-test" ? "Testing..." : "Test Proxy"}
            </button>
          </div>
        </Panel>

        <Panel title="Notifications" eyebrow="Master and per-category switches">
          <div className="toggleList">
            {[
              ["enabled", "Enable notifications"],
              ["heartbeat_alerts", "Heartbeat alerts"],
              ["success_alerts", "Success alerts"],
              ["error_alerts", "Error alerts"],
              ["worker_trace_enabled", "Worker Telegram trace"],
            ].map(([key, label]) => (
              <label className="toggleRow" key={key}>
                <span>{label}</span>
                <input
                  checked={Boolean(notifications[key])}
                  onChange={(event) =>
                    setNotifications((current) => ({ ...current, [key]: event.target.checked }))
                  }
                  type="checkbox"
                />
              </label>
            ))}
          </div>
          <div className="formGrid compact">
            <label className="field">
              <span>Trace level</span>
              <select
                onChange={(event) => setNotifications((current) => ({ ...current, worker_trace_level: event.target.value }))}
                value={notifications.worker_trace_level ?? "normal"}
              >
                <option value="minimal">minimal</option>
                <option value="normal">normal</option>
                <option value="verbose">verbose</option>
              </select>
            </label>
            <label className="field">
              <span>Queue update seconds</span>
              <input
                min="5"
                onChange={(event) =>
                  setNotifications((current) => ({
                    ...current,
                    worker_trace_queue_update_seconds: Number(event.target.value) || 60,
                  }))
                }
                type="number"
                value={notifications.worker_trace_queue_update_seconds ?? 60}
              />
            </label>
          </div>
          <div className="buttonRow">
            <button className="button primary" onClick={() => void saveNotificationsPanel()} type="button">
              {saving === "notifications" ? "Saving..." : "Save Notifications"}
            </button>
          </div>
        </Panel>

        <Panel title="Runtime Settings" eyebrow="Timer and action mode">
          <div className="formGrid compact">
            <label className="field checkboxField">
              <span>Enable timer</span>
              <input
                checked={timer.enabled}
                onChange={(event) => setTimer((current) => ({ ...current, enabled: event.target.checked }))}
                type="checkbox"
              />
            </label>
            <label className="field">
              <span>Interval</span>
              <input
                min="1"
                onChange={(event) => setTimer((current) => ({ ...current, interval: Number(event.target.value) || 1 }))}
                type="number"
                value={timer.interval}
              />
            </label>
            <label className="field">
              <span>Unit</span>
              <select
                onChange={(event) => setTimer((current) => ({ ...current, unit: event.target.value }))}
                value={timer.unit}
              >
                <option value="seconds">Seconds</option>
                <option value="minutes">Minutes</option>
                <option value="hours">Hours</option>
              </select>
            </label>
            <label className="field">
              <span>Action mode</span>
              <select
                onChange={(event) => setActionMode({ mode: event.target.value })}
                value={actionMode.mode}
              >
                <option value="off">off</option>
                <option value="notify_only">notify_only</option>
                <option value="selenium">selenium</option>
              </select>
            </label>
          </div>
          <div className="buttonRow">
            <button className="button primary" onClick={() => void saveRuntimePanels()} type="button">
              {saving === "runtime" ? "Saving..." : "Save Runtime Settings"}
            </button>
          </div>
        </Panel>

        <Panel title="Worker Controls" eyebrow="Queue protection and Selenium speed">
          <div className="formGrid compact">
            <label className="field checkboxField">
              <span>Queue check</span>
              <input
                checked={Boolean(workerSettings.queue_check_enabled)}
                onChange={(event) =>
                  setWorkerSettings((current) => ({ ...current, queue_check_enabled: event.target.checked }))
                }
                type="checkbox"
              />
            </label>
            <label className="field">
              <span>Queue timeout</span>
              <input
                min="1"
                onChange={(event) =>
                  setWorkerSettings((current) => ({
                    ...current,
                    queue_wait_timeout_seconds: Number(event.target.value) || 300,
                  }))
                }
                type="number"
                value={workerSettings.queue_wait_timeout_seconds ?? 300}
              />
            </label>
            <label className="field">
              <span>Queue poll</span>
              <input
                min="0.1"
                step="0.1"
                onChange={(event) =>
                  setWorkerSettings((current) => ({ ...current, queue_poll_seconds: Number(event.target.value) || 1 }))
                }
                type="number"
                value={workerSettings.queue_poll_seconds ?? 1}
              />
            </label>
            <label className="field">
              <span>Speed profile</span>
              <select
                onChange={(event) =>
                  setWorkerSettings((current) => ({ ...current, worker_speed_profile: event.target.value }))
                }
                value={workerSettings.worker_speed_profile ?? "balanced"}
              >
                {(workerSettings.worker_speed_profile_options ?? ["safe", "balanced", "fast", "custom"]).map((profile) => (
                  <option key={profile} value={profile}>
                    {profile}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Click pause</span>
              <input
                min="0"
                step="0.01"
                onChange={(event) =>
                  setWorkerSettings((current) => ({
                    ...current,
                    worker_click_pause_seconds: Number(event.target.value) || 0,
                  }))
                }
                type="number"
                value={workerSettings.worker_click_pause_seconds ?? 0.2}
              />
            </label>
            <label className="field">
              <span>Wait timeout</span>
              <input
                min="1"
                step="0.5"
                onChange={(event) =>
                  setWorkerSettings((current) => ({
                    ...current,
                    worker_wait_timeout_seconds: Number(event.target.value) || 20,
                  }))
                }
                type="number"
                value={workerSettings.worker_wait_timeout_seconds ?? 20}
              />
            </label>
            <label className="field">
              <span>Poll interval</span>
              <input
                min="0.05"
                step="0.01"
                onChange={(event) =>
                  setWorkerSettings((current) => ({
                    ...current,
                    worker_poll_seconds: Number(event.target.value) || 0.2,
                  }))
                }
                type="number"
                value={workerSettings.worker_poll_seconds ?? 0.2}
              />
            </label>
            <label className="field">
              <span>Retry pause</span>
              <input
                min="0"
                step="0.01"
                onChange={(event) =>
                  setWorkerSettings((current) => ({
                    ...current,
                    worker_retry_pause_seconds: Number(event.target.value) || 0,
                  }))
                }
                type="number"
                value={workerSettings.worker_retry_pause_seconds ?? 0.45}
              />
            </label>
            <label className="field">
              <span>After navigation</span>
              <input
                min="0"
                step="0.05"
                onChange={(event) =>
                  setWorkerSettings((current) => ({
                    ...current,
                    worker_after_navigation_wait_seconds: Number(event.target.value) || 0,
                  }))
                }
                type="number"
                value={workerSettings.worker_after_navigation_wait_seconds ?? 0.5}
              />
            </label>
            <label className="field">
              <span>After add-to-cart</span>
              <input
                min="0"
                step="0.05"
                onChange={(event) =>
                  setWorkerSettings((current) => ({
                    ...current,
                    worker_after_add_to_cart_wait_seconds: Number(event.target.value) || 0,
                  }))
                }
                type="number"
                value={workerSettings.worker_after_add_to_cart_wait_seconds ?? 0.6}
              />
            </label>
            <label className="field">
              <span>After checkout click</span>
              <input
                min="0"
                step="0.05"
                onChange={(event) =>
                  setWorkerSettings((current) => ({
                    ...current,
                    worker_after_checkout_click_wait_seconds: Number(event.target.value) || 0,
                  }))
                }
                type="number"
                value={workerSettings.worker_after_checkout_click_wait_seconds ?? 0.6}
              />
            </label>
          </div>
          <div className="buttonRow">
            <button className="button primary" onClick={() => void saveRuntimePanels()} type="button">
              {saving === "runtime" ? "Saving..." : "Save Worker Settings"}
            </button>
          </div>
        </Panel>

        <Panel title="DB Tools" eyebrow="Practical maintenance">
          <dl className="detailList">
            <div>
              <dt>Path</dt>
              <dd className="breakableText">{dbStatus?.path ?? "-"}</dd>
            </div>
            <div>
              <dt>Exists</dt>
              <dd>{dbStatus?.exists ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Size</dt>
              <dd>{dbStatus?.size_bytes ?? 0} bytes</dd>
            </div>
          </dl>
          <div className="buttonRow">
            <button className="button primary" onClick={() => void backupDatabase()} type="button">
              {saving === "backup" ? "Backing up..." : "Backup DB"}
            </button>
            <button className="button" onClick={() => void clearOldLogs()} type="button">
              {saving === "clear-logs" ? "Cleaning..." : "Clear Old Logs"}
            </button>
            <button className="button danger" onClick={() => void clearStaleActions()} type="button">
              {saving === "clear-actions" ? "Cleaning..." : "Clear Stale Actions"}
            </button>
          </div>
        </Panel>
      </div>

      <Panel
        title="Credentials"
        eyebrow=".env-backed full editor"
        action={
          <label className="checkboxRow">
            <input checked={showSensitive} onChange={(event) => setShowSensitive(event.target.checked)} type="checkbox" />
            <span>Show sensitive values</span>
          </label>
        }
      >
        <div className="credentialsGrid">
          {Object.entries(groupedCredentials).map(([group, items]) => (
            <section className="credentialSection" key={group}>
              <div className="subHeader">
                <h4>{group}</h4>
              </div>
              <div className="formGrid compact">
                {items.map((item) => (
                  <label className="field" key={item.key}>
                    <span>{item.label}</span>
                    {item.field_type === "boolean" ? (
                      <input
                        checked={Boolean(credentialValues[item.key])}
                        onChange={(event) =>
                          setCredentialValues((current) => ({ ...current, [item.key]: event.target.checked }))
                        }
                        type="checkbox"
                      />
                    ) : (
                      <input
                        onChange={(event) =>
                          setCredentialValues((current) => ({ ...current, [item.key]: event.target.value }))
                        }
                        type={item.sensitive && !showSensitive ? "password" : item.field_type === "number" ? "number" : "text"}
                        value={credentialValues[item.key] ?? ""}
                      />
                    )}
                    <small>{item.key}</small>
                  </label>
                ))}
              </div>
            </section>
          ))}
        </div>
        <div className="buttonRow">
          <button className="button primary" onClick={() => void saveCredentialsPanel()} type="button">
            {saving === "credentials" ? "Saving..." : "Save Credentials"}
          </button>
          <button className="button" onClick={() => void loadAll()} type="button">
            Reload
          </button>
        </div>
      </Panel>
    </div>
  );
}

function FAQPage() {
  return (
    <div className="pageStack">
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Owner-friendly FAQ</p>
          <h2>Simple explanations for the parts most likely to cause confusion during everyday use.</h2>
        </div>
      </section>
      <div className="faqStack">
        {FAQ_ITEMS.map((item) => (
          <article className="faqCard" key={item.title}>
            <h3>{item.title}</h3>
            <p>{item.body}</p>
          </article>
        ))}
      </div>
    </div>
  );
}

export default function App() {
  const [activePage, setActivePage] = useState("dashboard");
  const [theme, setTheme] = useState(resolveInitialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.body.dataset.theme = theme;
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  return (
    <Shell
      activePage={activePage}
      onSelect={setActivePage}
      theme={theme}
      onToggleTheme={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
    >
      {activePage === "dashboard" ? <DashboardPage /> : null}
      {activePage === "runtime" ? <RuntimePage /> : null}
      {activePage === "scan-settings" ? <ScanSettingsPage /> : null}
      {activePage === "watchlist" ? <WatchlistPage /> : null}
      {activePage === "filters" ? <FiltersPage /> : null}
      {activePage === "logs" ? <LogsPage /> : null}
      {activePage === "settings" ? <SettingsPage /> : null}
      {activePage === "faq" ? <FAQPage /> : null}
    </Shell>
  );
}
