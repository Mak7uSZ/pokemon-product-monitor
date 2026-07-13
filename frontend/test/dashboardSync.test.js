import assert from "node:assert/strict";
import test from "node:test";

import { createDashboardSync, createRevisionGate, topicForPath } from "../src/dashboardSync.js";

class FakeBroadcastChannel {
  static channels = new Map();

  constructor(name) {
    this.name = name;
    this.listeners = new Set();
    const peers = FakeBroadcastChannel.channels.get(name) ?? new Set();
    peers.add(this);
    FakeBroadcastChannel.channels.set(name, peers);
  }

  addEventListener(_type, listener) {
    this.listeners.add(listener);
  }

  postMessage(message) {
    for (const peer of FakeBroadcastChannel.channels.get(this.name) ?? []) {
      if (peer !== this) {
        peer.listeners.forEach((listener) => listener({ data: message }));
      }
    }
  }

  close() {
    FakeBroadcastChannel.channels.get(this.name)?.delete(this);
  }
}

function fakeWindow() {
  return {
    BroadcastChannel: FakeBroadcastChannel,
    addEventListener() {},
    removeEventListener() {},
    localStorage: { setItem() {} },
  };
}

test("a committed mutation in tab A invalidates tab B once", () => {
  const tabA = createDashboardSync(fakeWindow());
  const tabB = createDashboardSync(fakeWindow());
  const messages = [];
  tabB.subscribe((message) => messages.push(message));

  tabA.publish("watchlist", { method: "PATCH" });

  assert.equal(messages.length, 1);
  assert.equal(messages[0].topic, "watchlist");
  tabA.close();
  tabB.close();
});

test("duplicate events are harmless", () => {
  const tab = createDashboardSync(fakeWindow());
  let deliveries = 0;
  tab.subscribe(() => {
    deliveries += 1;
  });
  const message = tab.publish("filters");
  for (const peer of FakeBroadcastChannel.channels.values()) {
    peer.forEach((channel) => channel.listeners.forEach((listener) => listener({ data: message })));
  }
  assert.equal(deliveries, 1);
  tab.close();
});

test("revision gate rejects delayed out-of-order responses", () => {
  const gate = createRevisionGate();
  const slow = gate.issue();
  const fast = gate.issue();
  assert.equal(gate.isLatest(fast), true);
  assert.equal(gate.isLatest(slow), false);
});

test("API paths map to stable invalidation topics", () => {
  assert.equal(topicForPath("/api/watchlist/1?x=1"), "watchlist");
  assert.equal(topicForPath("/api/runtime/status"), "runtime");
});
