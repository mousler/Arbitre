import { analyzeVideo, exploreDebates, getServer, normalizeServer, openRhetora, youtubeVideoId } from "./shared.js";

const $ = id => document.getElementById(id);

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const id = youtubeVideoId(tab?.url || "");
  const video = $("video");
  if (id) {
    const title = (tab.title || "").replace(/^\(\d+\)\s*/, "").replace(/\s*-\s*YouTube$/, "");
    video.textContent = "";
    const name = document.createElement("b");
    name.textContent = title || `Vidéo ${id}`;
    video.append("Vidéo détectée : ", name);
    $("analyze").disabled = false;
    $("analyze").addEventListener("click", async () => { await analyzeVideo(id); window.close(); });
  } else {
    video.textContent = "Ouvrez une vidéo YouTube pour l’analyser, ou faites un clic droit sur un lien YouTube.";
  }
  $("server").value = await getServer();
}

$("search").addEventListener("submit", async event => {
  event.preventDefault();
  const query = $("query").value.trim();
  if (!query) return;
  await exploreDebates(query);
  window.close();
});

$("open").addEventListener("click", async () => { await openRhetora(""); window.close(); });

$("settings").addEventListener("submit", async event => {
  event.preventDefault();
  const server = normalizeServer($("server").value);
  if (!server) {
    $("settings-note").textContent = "Adresse invalide : utilisez par exemple http://localhost:8766.";
    return;
  }
  await chrome.storage.sync.set({ server });
  $("server").value = server;
  $("settings-note").textContent = "Adresse enregistrée.";
});

init();
