// Publishes ipad/site/ to GitHub Pages (free HTTPS hosting), so testers can install the app from Safari.
//
//   node deploy.mjs [--repo owner/name]        (default: <your GitHub user>/live-translator-ipad)
//
// Needs the GitHub CLI (gh) signed in. The repository is created PUBLIC if it does not exist
// (GitHub Pages is free only for public repositories). It holds only the built web app and the speech
// models — never an API key: each tester types their own key on the iPad.
// The whole site/ folder (every build ever made) is pushed, so older builds stay available.
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const SITE = path.join(path.dirname(fileURLToPath(import.meta.url)), "site");
const args = process.argv.slice(2);
const sh = (cmd, a, opts = {}) => execFileSync(cmd, a, { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"], ...opts }).trim();

if (!fs.existsSync(path.join(SITE, "releases.json"))) {
  console.error("Nothing to publish: run `node build.mjs` first.");
  process.exit(1);
}
const user = sh("gh", ["api", "user", "--jq", ".login"]);
const at = args.indexOf("--repo");
const repo = at >= 0 && args[at + 1] ? args[at + 1] : `${user}/live-translator-ipad`;

let exists = true;
try { sh("gh", ["repo", "view", repo, "--json", "name"]); } catch { exists = false; }
if (!exists) {
  console.log(`Creating public repository ${repo} ...`);
  sh("gh", ["repo", "create", repo, "--public", "--description", "Live Translator for iPad (web app)"]);
}

// a throw-away git repo inside site/ holding exactly the site, pushed to the gh-pages branch.
// It is put on top of what is already published (fetched without file contents), so only new or
// changed files are uploaded — re-sending the ~250 MB of models ran the machine out of memory.
const git = (...a) => sh("git", ["-C", SITE, ...a]);
const remote = `https://github.com/${repo}.git`;
fs.rmSync(path.join(SITE, ".git"), { recursive: true, force: true });
git("init", "-q", "-b", "gh-pages");
let published = true;
try { git("fetch", "-q", "--filter=blob:none", "--depth=1", remote, "gh-pages"); } catch { published = false; }
if (published) git("reset", "-q", "--soft", "FETCH_HEAD");
git("add", "-A");
const builds = JSON.parse(fs.readFileSync(path.join(SITE, "releases.json"), "utf8")).releases.map((r) => `${r.version}-${r.build}`);
let changed = true;
try {
  git("-c", "user.name=Live Translator", "-c", `user.email=${user}@users.noreply.github.com`,
      "commit", "-q", "-m", `Publish ${builds[builds.length - 1]}`);
} catch { changed = false; }                               // nothing new to publish
if (changed) {
  console.log(published ? "Uploading the new files ..." : "Uploading (first time: ~250 MB of models, a few minutes) ...");
  execFileSync("git", ["-C", SITE, "-c", "http.postBuffer=157286400", "push", "-q", remote, "HEAD:gh-pages"], { stdio: "inherit" });
} else {
  console.log("Nothing new to publish.");
}
fs.rmSync(path.join(SITE, ".git"), { recursive: true, force: true });

try {
  sh("gh", ["api", "-X", "POST", `repos/${repo}/pages`, "-f", "source[branch]=gh-pages", "-f", "source[path]=/"]);
} catch { /* Pages already enabled */ }
const [owner, name] = repo.split("/");
const url = `https://${owner.toLowerCase()}.github.io/${name}/`;
console.log(`\nDone. In 1-2 minutes the app is at:\n  ${url}\nInvite testers with:\n  ${url}install.html`);
