let viewerId = null;

const DEFAULTS = {
  ingestUrl: "http://127.0.0.1:8081/api/ingest",
  ingestSecret: "",
  syncEnabled: true,
  seconds: 30
};

async function ensureRefresh() {
  const { seconds } = await chrome.storage.local.get("seconds");
  // Old default was 60. Move unset or that default to 30; leave any other choice.
  if (seconds == null || Number(seconds) === 60) {
    await chrome.storage.local.set({ seconds: DEFAULTS.seconds, nextRefresh: 0 });
  }
}

chrome.action.onClicked.addListener(async () => {
  if (viewerId !== null) {
    try {
      await chrome.windows.update(viewerId, { focused: true });
      return;
    } catch (e) { viewerId = null; }
  }
  const win = await chrome.windows.create({
    url: chrome.runtime.getURL("viewer.html"),
    type: "popup",
    width: 1100,
    height: 760
  });
  viewerId = win.id;
});

chrome.windows.onRemoved.addListener((id) => {
  if (id === viewerId) viewerId = null;
});

async function flushPending() {
  const cfg = await chrome.storage.local.get([
    "pendingSync", "syncEnabled", "ingestUrl", "ingestSecret", "lastSync", "lastSyncError"
  ]);
  const enabled = cfg.syncEnabled !== false;
  if (!enabled) return;
  const pending = cfg.pendingSync || [];
  if (!pending.length) return;
  const url = cfg.ingestUrl || DEFAULTS.ingestUrl;
  const headers = { "Content-Type": "application/json" };
  if (cfg.ingestSecret) headers["X-Ingest-Secret"] = cfg.ingestSecret;
  // Send up to 50 at a time
  const batch = pending.slice(0, 50);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ posts: batch })
    });
    const text = await res.text();
    let body = {};
    try { body = JSON.parse(text); } catch (_) { body = { raw: text.slice(0, 200) }; }
    if (!res.ok) {
      await chrome.storage.local.set({
        lastSyncError: `HTTP ${res.status}: ${body.error || text.slice(0, 120)}`,
        lastSync: Date.now()
      });
      return;
    }
    // Drop successfully attempted batch (accepted + duplicates are both "seen")
    const rest = pending.slice(batch.length);
    await chrome.storage.local.set({
      pendingSync: rest,
      lastSync: Date.now(),
      lastSyncError: null,
      lastSyncResult: body
    });
  } catch (e) {
    await chrome.storage.local.set({
      lastSyncError: String(e.message || e).slice(0, 200),
      lastSync: Date.now()
    });
  }
}

chrome.alarms.create("xtracker-ingest", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "xtracker-ingest") flushPending();
});

chrome.storage.onChanged.addListener((c) => {
  if (c.pendingSync && (c.pendingSync.newValue || []).length) {
    flushPending();
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "flush") {
    flushPending().then(() => sendResponse({ ok: true })).catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
});

// Seed defaults once
chrome.runtime.onInstalled.addListener(async () => {
  const cur = await chrome.storage.local.get(["ingestUrl", "syncEnabled"]);
  const patch = {};
  if (cur.ingestUrl == null) patch.ingestUrl = DEFAULTS.ingestUrl;
  if (cur.syncEnabled == null) patch.syncEnabled = true;
  if (Object.keys(patch).length) await chrome.storage.local.set(patch);
  await ensureRefresh();
});
ensureRefresh();
