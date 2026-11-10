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

// a throw-away git repo inside site/ holding exactly the site, pushed to the gh-pages branch
const git = (...a) => sh("git", ["-C", SITE, ...a]);
fs.rmSync(path.join(SITE, ".git"), { recursive: true, force: true });
git("init", "-q", "-b", "gh-pages");
git("add", "-A");
git("-c", "user.name=Live Translator", "-c", `user.email=${user}@users.noreply.github.com`,
    "commit", "-q", "-m", `Publish ${JSON.parse(fs.readFileSync(path.join(SITE, "releases.json"), "utf8")).releases.map((r) => `${r.version}-${r.build}`).join(", ")}`);
console.log("Uploading (the first time takes a few minutes: ~250 MB of models) ...");
execFileSync("git", ["-C", SITE, "push", "-q", "--force", `https://github.com/${repo}.git`, "gh-pages"], { stdio: "inherit" });
fs.rmSync(path.join(SITE, ".git"), { recursive: true, force: true });

try {
  sh("gh", ["api", "-X", "POST", `repos/${repo}/pages`, "-f", "source[branch]=gh-pages", "-f", "source[path]=/"]);
} catch { /* Pages already enabled */ }
const [owner, name] = repo.split("/");
const url = `https://${owner.toLowerCase()}.github.io/${name}/`;
console.log(`\nDone. In 1-2 minutes the app is at:\n  ${url}\nInvite testers with:\n  ${url}install.html`);
