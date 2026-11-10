// TestFlight-style test channel without the App Store:
//  * every build stays online at v/<version>-<build>/ (testers can go back to an older one)
//  * releases.json lists the builds with notes and their channel (beta / stable)
//  * the app checks it and offers "update" when a newer build exists for the tester's channel
//  * testers can choose the Beta channel or stable builds only, pin a build, and send feedback
import { APP_VERSION, BUILD, CHANNEL } from "./version.js";

export const ROOT = new URL("../../../", import.meta.url).href;
export const BUILD_ID = `${APP_VERSION}-${BUILD}`;
export const BUILD_LABEL = `v${APP_VERSION} (${BUILD})${CHANNEL === "beta" ? " BETA" : ""}`;
const KEY = "lt_update";

export function getPrefs() {
  try {
    return { channel: "beta", pinned: null, ...JSON.parse(localStorage.getItem(KEY) || "{}") };
  } catch {
    return { channel: "beta", pinned: null };
  }
}

export function setPrefs(p) {
  try { localStorage.setItem(KEY, JSON.stringify({ ...getPrefs(), ...p })); } catch { /* private mode */ }
}

function vnum(v) {
  return v.split(".").map((x) => parseInt(x, 10) || 0);
}

/** Compare two releases by version, then build number. */
export function cmp(a, b) {
  const x = vnum(a.version), y = vnum(b.version);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) - (y[i] || 0);
  }
  return a.build - b.build;
}

export const idOf = (r) => `${r.version}-${r.build}`;

export async function fetchReleases() {
  try {
    const r = await fetch(ROOT + "releases.json", { cache: "no-store" });
    if (r.ok) return await r.json();
  } catch { /* offline */ }
  const hit = await caches.match(ROOT + "releases.json").catch(() => null);
  return hit ? hit.json() : { releases: [] };
}

/** Builds this tester can see (Beta channel sees everything), newest first. */
export function visible(data, channel) {
  return (data.releases || []).filter((r) => channel === "beta" || r.channel === "stable").sort((a, b) => cmp(b, a));
}

/** Newer build than the running one for this channel, or null. */
export function newerBuild(data, channel) {
  const top = visible(data, channel)[0];
  return top && cmp(top, { version: APP_VERSION, build: BUILD }) > 0 ? top : null;
}

/** Open a build (null = always the newest for the channel). */
export function openBuild(id) {
  setPrefs({ pinned: id });
  location.replace(ROOT + (id ? `v/${id}/` : ""));
}

export function diagnostics(extra = {}) {
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  return {
    app: "Live Translator iPad", build: BUILD_ID, channel: getPrefs().channel,
    device: navigator.userAgent, screen: `${screen.width}x${screen.height}@${devicePixelRatio}`,
    installed: standalone, time: new Date().toISOString(), ...extra,
  };
}
