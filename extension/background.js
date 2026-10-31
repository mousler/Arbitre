import { analyzeVideo, exploreDebates, youtubeVideoId } from "./shared.js";

const VIDEO_PAGES = ["*://*.youtube.com/watch*", "*://*.youtube.com/shorts/*", "*://*.youtube.com/live/*"];
const VIDEO_LINKS = [...VIDEO_PAGES, "*://youtu.be/*"];

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "analyze-page", title: "Analyser ce débat avec Rhetora", contexts: ["page", "video"], documentUrlPatterns: VIDEO_PAGES });
    chrome.contextMenus.create({ id: "analyze-link", title: "Analyser cette vidéo avec Rhetora", contexts: ["link"], targetUrlPatterns: VIDEO_LINKS });
    chrome.contextMenus.create({ id: "explore-selection", title: "Chercher « %s » dans les débats Rhetora", contexts: ["selection"] });
  });
});

async function warn(tabId) {
  await chrome.action.setBadgeBackgroundColor({ color: "#d55d9e", tabId });
  await chrome.action.setBadgeText({ text: "?", tabId });
  await chrome.action.setTitle({ title: "Aucune vidéo YouTube dans cet onglet", tabId });
  setTimeout(() => chrome.action.setBadgeText({ text: "", tabId }).catch(() => {}), 3000);
}

async function analyzeUrl(url, tabId) {
  const id = youtubeVideoId(url || "");
  if (id) await analyzeVideo(id);
  else if (tabId) await warn(tabId);
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "explore-selection") exploreDebates(info.selectionText || "");
  else analyzeUrl(info.menuItemId === "analyze-link" ? info.linkUrl : info.pageUrl, tab?.id);
});

chrome.commands.onCommand.addListener((command, tab) => {
  if (command === "analyze-tab") analyzeUrl(tab?.url, tab?.id);
});

const escapeXml = text => text.replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" })[char]);

chrome.omnibox.setDefaultSuggestion({ description: "Chercher des débats sur Rhetora" });
chrome.omnibox.onInputChanged.addListener(text => {
  chrome.omnibox.setDefaultSuggestion({
    description: youtubeVideoId(text.trim())
      ? "Analyser cette vidéo YouTube avec Rhetora"
      : `Chercher des débats sur Rhetora : <match>${escapeXml(text.trim())}</match>`
  });
});
chrome.omnibox.onInputEntered.addListener(text => {
  const id = youtubeVideoId(text.trim());
  if (id) analyzeVideo(id);
  else if (text.trim()) exploreDebates(text);
});
