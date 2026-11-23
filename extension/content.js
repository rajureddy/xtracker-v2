// Runs on x.com. On /notifications it reads alerts; on /home or /i/timeline the feed.
// Results go to chrome.storage.local; background.js POSTs new rows to xtracker-v2.
const MAX_POSTS = 500;
let refreshTimer = null;

const page = () => {
  const p = location.pathname;
  if (p.startsWith("/notifications")) return "notifications";
  if (p === "/home") return "home";
  if (p.startsWith("/i/timeline")) return "timeline";
  return null;
};
const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

function hash(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return "n" + Math.abs(h);
}

function avatarFrom(el) {
  const img =
    el?.querySelector('[data-testid="Tweet-User-Avatar"] img') ||
    el?.querySelector('img[src*="profile_images"]') ||
    el?.querySelector('img[src*="pbs.twimg.com"]');
  const src = img?.getAttribute("src") || "";
  return src && src.startsWith("http") ? src.replace("_normal.", "_bigger.") : "";
}

function parseTweet(a) {
  const timeEl = a.querySelector("time");
  const href = timeEl?.closest("a")?.getAttribute("href") || "";
  const m = href.match(/^\/([^/]+)\/status\/(\d+)/);
  if (!m) return null;
  const count = (id) => (a.querySelector(`[data-testid="${id}"]`)?.innerText || "").trim() || "0";
  return {
    id: m[2], handle: "@" + m[1],
    name: a.querySelector('[data-testid="User-Name"] span')?.innerText || "",
    text: a.querySelector('[data-testid="tweetText"]')?.innerText || "",
    time: timeEl.getAttribute("datetime"),
    replies: count("reply"), reposts: count("retweet"), likes: count("like"),
    avatar: avatarFrom(a),
    url: "https://x.com" + href,
    source: page() || "timeline"
  };
}

function parseNotification(n) {
  const label = clean(n.innerText);
  if (!label) return null;
  const link = n.querySelector('a[href*="/status/"]');
  const href = link?.getAttribute("href") || "";
  const m = href.match(/^\/([^/]+)\/status\/(\d+)/);
  const tweetText = n.querySelector('[data-testid="tweetText"]')?.innerText || "";
  return {
    id: m ? m[2] : hash(label),
    handle: m ? "@" + m[1] : "",
    name: "",
    text: tweetText || label,
    alert: tweetText ? label.replace(tweetText, "").trim() : "",
    time: n.querySelector("time")?.getAttribute("datetime") || null,
    replies: "", reposts: "", likes: "",
    avatar: avatarFrom(n),
    url: m ? "https://x.com" + href : "",
    source: "notifications"
  };
}

let lastDebug = "";
async function report(pageName, count) {
  const d = JSON.stringify({ pageName, count, path: location.pathname });
  if (d === lastDebug) return;
  lastDebug = d;
  await chrome.storage.local.set({ debug: { pageName, count, path: location.pathname, at: Date.now() } });
}

async function scan() {
  const p = page();
  if (!p) { report("other", 0); return; }
  let nodes;
  if (p === "notifications") {
    nodes = [...document.querySelectorAll('article[data-testid="notification"]')];
    if (!nodes.length) nodes = [...document.querySelectorAll('[data-testid="cellInnerDiv"]')];
  } else {
    nodes = [...document.querySelectorAll('article[data-testid="tweet"]')];
  }
  const found = nodes.map(p === "notifications" ? parseNotification : parseTweet).filter(Boolean);
  report(p, found.length);
  if (!found.length) return;

  const { posts = {}, pendingSync = [] } = await chrome.storage.local.get(["posts", "pendingSync"]);
  const now = Date.now();
  let added = 0;
  const fresh = [];
  for (const f of found) {
    const old = posts[f.id];
    if (!old) {
      added++;
      fresh.push(f);
    }
    f.seen = old ? old.seen : now;
    if (!f.time) f.time = old ? old.time : new Date(now).toISOString();
    posts[f.id] = f;
  }
  const trimmed = Object.values(posts)
    .sort((a, b) => new Date(b.time) - new Date(a.time))
    .slice(0, MAX_POSTS);
  const update = { posts: Object.fromEntries(trimmed.map((x) => [x.id, x])) };
  if (added) {
    update.lastScan = now;
    // Queue only brand-new ids for ingest (cap pending)
    const pending = [...pendingSync, ...fresh].slice(-200);
    update.pendingSync = pending;
  }
  await chrome.storage.local.set(update);
}

function refresh() {
  const p = page();
  if (!p) return;
  window.scrollTo({ top: 0 });
  if (p === "home" || p === "timeline") {
    const pill = [...document.querySelectorAll('button, [role="button"]')]
      .find((b) => /^show \d+\+? posts?$/i.test(b.innerText.trim()));
    if (pill) pill.click();
    else if (p === "home") document.querySelector('[data-testid="AppTabBar_Home_Link"]')?.click();
    else { location.reload(); return; }
  } else {
    document.querySelector('[data-testid="AppTabBar_Notifications_Link"]')?.click();
  }
  setTimeout(scan, 4000);
}

const minGap = () => (page() === "timeline" ? 30 : 15);

async function schedule(fromStart) {
  clearTimeout(refreshTimer);
  const { running = false, seconds = 30, nextRefresh = 0 } =
    await chrome.storage.local.get(["running", "seconds", "nextRefresh"]);
  if (!running) return;
  const gap = Math.max(minGap(), seconds) * 1000;
  const wait = fromStart ? 0 : nextRefresh > Date.now() ? nextRefresh - Date.now() : gap;
  refreshTimer = setTimeout(tick, wait);
}

async function tick() {
  const { seconds = 30 } = await chrome.storage.local.get("seconds");
  await chrome.storage.local.set({ nextRefresh: Date.now() + Math.max(minGap(), seconds) * 1000 });
  refresh();
  schedule(false);
}

chrome.storage.onChanged.addListener((c) => {
  if (c.running) schedule(!!c.running.newValue);
  else if (c.seconds) chrome.storage.local.set({ nextRefresh: 0 }).then(() => schedule(false));
});

schedule(false);
scan();
setInterval(scan, 5000);
