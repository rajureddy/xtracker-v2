const $ = (id) => document.getElementById(id);
let current = [];

function cell(tr, cls, content) {
  const td = document.createElement("td");
  if (cls) td.className = cls;
  if (content instanceof Node) td.appendChild(content); else td.textContent = content;
  tr.appendChild(td);
  return td;
}

function render(st) {
  const {
    posts = {}, running = false, seconds = 30, lastScan, debug,
    pendingSync = [], lastSync, lastSyncError, lastSyncResult,
    syncEnabled = true, ingestUrl, ingestSecret
  } = st;

  $("toggle").textContent = running ? "Stop" : "Start";
  $("toggle").classList.toggle("on", running);
  if (document.activeElement !== $("secs")) $("secs").value = seconds;
  if (document.activeElement !== $("ingestUrl") && ingestUrl != null) $("ingestUrl").value = ingestUrl;
  if (document.activeElement !== $("ingestSecret") && ingestSecret != null) $("ingestSecret").value = ingestSecret;
  $("syncOn").checked = syncEnabled !== false;

  current = Object.values(posts).sort((a, b) => new Date(b.time) - new Date(a.time));
  $("tbl").hidden = current.length === 0;
  $("empty").hidden = current.length > 0;

  const body = $("body");
  body.replaceChildren();
  const freshCutoff = (lastScan || 0) - 1000;
  for (const p of current) {
    const tr = document.createElement("tr");
    if (p.seen >= freshCutoff && lastScan) tr.className = "new";
    cell(tr, "time", new Date(p.time).toLocaleString());
    const user = cell(tr, "user", p.name);
    const h = document.createElement("span");
    h.className = "h"; h.textContent = p.handle;
    user.appendChild(h);
    cell(tr, "post", p.alert ? p.alert + "\n" + p.text : p.text);
    cell(tr, "num", p.replies);
    cell(tr, "num", p.reposts);
    cell(tr, "num", p.likes);
    if (!p.url) { cell(tr, "", ""); body.appendChild(tr); continue; }
    const a = document.createElement("a");
    a.href = p.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = "Open";
    cell(tr, "", a);
    body.appendChild(tr);
  }
  const fresh = debug && Date.now() - debug.at < 10 * 60 * 1000;
  const tab = !debug
    ? "No X tab detected: reload your x.com tab"
    : `X tab: ${debug.pageName} (${debug.path}), ${debug.count} items on screen`;
  $("status").textContent =
    `${current.length} saved` + (lastScan ? ` · new at ${new Date(lastScan).toLocaleTimeString()}` : "") + ` · ${tab}`;

  const syncEl = $("syncStatus");
  const pending = pendingSync.length;
  let msg = `pending ${pending}`;
  if (lastSync) msg += ` · last sync ${new Date(lastSync).toLocaleTimeString()}`;
  if (lastSyncResult) msg += ` · ok +${lastSyncResult.accepted || 0} dup ${lastSyncResult.duplicates || 0}`;
  syncEl.textContent = msg + (lastSyncError ? ` · ${lastSyncError}` : "");
  syncEl.className = lastSyncError ? "bad" : "ok";
}

async function load() {
  render(await chrome.storage.local.get([
    "posts", "running", "seconds", "lastScan", "debug",
    "pendingSync", "lastSync", "lastSyncError", "lastSyncResult",
    "syncEnabled", "ingestUrl", "ingestSecret"
  ]));
}

$("toggle").onclick = async () => {
  const { running = false } = await chrome.storage.local.get("running");
  await chrome.storage.local.set({ running: !running });
};
$("secs").onchange = () =>
  chrome.storage.local.set({ seconds: Math.max(15, Number($("secs").value) || 30) });
$("clear").onclick = () => chrome.storage.local.set({ posts: {}, lastScan: null, pendingSync: [] });
$("csv").onclick = () => {
  const esc = (v) => `"${String(v).replace(/"/g, '""')}"`;
  const rows = [["Posted", "Name", "Handle", "Post", "Replies", "Reposts", "Likes", "URL"]]
    .concat(current.map((p) => [p.time, p.name, p.handle, p.text, p.replies, p.reposts, p.likes, p.url]));
  const blob = new Blob([rows.map((r) => r.map(esc).join(",")).join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "x-timeline-v2.csv";
  a.click();
};

$("syncOn").onchange = () => chrome.storage.local.set({ syncEnabled: $("syncOn").checked });
$("ingestUrl").onchange = () => chrome.storage.local.set({ ingestUrl: $("ingestUrl").value.trim() });
$("ingestSecret").onchange = () => chrome.storage.local.set({ ingestSecret: $("ingestSecret").value });
$("syncNow").onclick = async () => {
  // Re-queue all saved posts that have text, then wake the service worker alarm path
  const { posts = {}, pendingSync = [] } = await chrome.storage.local.get(["posts", "pendingSync"]);
  const all = Object.values(posts);
  const have = new Set(pendingSync.map((p) => p.id));
  const extra = all.filter((p) => p.text && !have.has(p.id));
  await chrome.storage.local.set({ pendingSync: [...pendingSync, ...extra].slice(-200) });
  chrome.runtime.sendMessage({ type: "flush" }).catch(() => {});
};

chrome.runtime.onMessage.addListener(() => {});
chrome.storage.onChanged.addListener(load);
load();
