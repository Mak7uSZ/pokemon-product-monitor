const CHANNEL_NAME = "pokemon-parser-dashboard-v1";
const STORAGE_KEY = "pokemon-parser-dashboard-sync";

export function topicForPath(path) {
  const parts = String(path || "")
    .split("?")[0]
    .split("/")
    .filter(Boolean);
  if (parts[0] !== "api") {
    return "dashboard";
  }
  return parts[1] || "dashboard";
}

export function createRevisionGate() {
  let latest = 0;
  return {
    issue() {
      latest += 1;
      return latest;
    },
    isLatest(revision) {
      return revision === latest;
    },
    current() {
      return latest;
    },
  };
}

export function createDashboardSync(windowObject = globalThis.window) {
  const listeners = new Set();
  const seen = new Set();
  const instanceId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  let sequence = 0;
  let channel = null;

  function deliver(message) {
    if (!message || typeof message !== "object" || !message.id || seen.has(message.id)) {
      return;
    }
    seen.add(message.id);
    if (seen.size > 500) {
      seen.delete(seen.values().next().value);
    }
    listeners.forEach((listener) => listener(message));
  }

  if (windowObject && typeof windowObject.BroadcastChannel === "function") {
    channel = new windowObject.BroadcastChannel(CHANNEL_NAME);
    channel.addEventListener("message", (event) => deliver(event.data));
  }

  function onStorage(event) {
    if (event.key !== STORAGE_KEY || !event.newValue) {
      return;
    }
    try {
      deliver(JSON.parse(event.newValue));
    } catch (_error) {
      // Ignore malformed messages written by unrelated or older clients.
    }
  }

  windowObject?.addEventListener?.("storage", onStorage);

  return {
    publish(topic, details = {}) {
      const message = {
        id: `${instanceId}:${++sequence}`,
        topic: String(topic || "dashboard"),
        committedAt: Date.now(),
        details,
      };
      deliver(message);
      channel?.postMessage(message);
      try {
        windowObject?.localStorage?.setItem(STORAGE_KEY, JSON.stringify(message));
      } catch (_error) {
        // BroadcastChannel is sufficient when storage is unavailable.
      }
      return message;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    close() {
      listeners.clear();
      channel?.close();
      windowObject?.removeEventListener?.("storage", onStorage);
    },
  };
}
