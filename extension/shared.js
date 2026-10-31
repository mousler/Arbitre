export const DEFAULT_SERVER = "http://localhost:8766";
const YOUTUBE_HOSTS = new Set(["youtube.com", "youtu.be"]);

export function youtubeVideoId(raw) {
  let url;
  try { url = new URL(raw); } catch { return ""; }
  if (!/^https?:$/.test(url.protocol) || !YOUTUBE_HOSTS.has(url.hostname.replace(/^(www|m)\./, ""))) return "";
  return (url.href.match(/(?:v=|youtu\.be\/|shorts\/|embed\/|live\/)([\w-]{11})/) || [])[1] || "";
}

export function normalizeServer(raw) {
  let url;
  try { url = new URL(String(raw || "").trim()); } catch { return ""; }
  return /^https?:$/.test(url.protocol) ? url.origin : "";
}

export async function getServer() {
  const { server } = await chrome.storage.sync.get("server");
  return normalizeServer(server) || DEFAULT_SERVER;
}

// Réutilise l'onglet Rhetora ouvert : chaque onglet a sa propre file d'analyse.
export async function openRhetora(hash) {
  const base = await getServer();
  const page = `${base}/arbitre.html`;
  const target = `${page}${hash}`;
  const tabs = await chrome.tabs.query({});
  const existing = tabs.find(tab => tab.url && tab.url.startsWith(page));
  if (existing) {
    await chrome.tabs.update(existing.id, { url: target, active: true });
    await chrome.windows.update(existing.windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url: target });
  }
}

export function analyzeVideo(id) {
  return openRhetora(`#/analyser?url=${encodeURIComponent(`https://www.youtube.com/watch?v=${id}`)}`);
}

export function exploreDebates(query) {
  return openRhetora(`#/explorer?q=${encodeURIComponent(query.trim().slice(0, 150))}`);
}
