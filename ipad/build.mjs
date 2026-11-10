// Builds the iPad web app into ipad/site/ (upload that folder to any HTTPS static host).
//
//   node build.mjs [--models <folder with vosk-en-small and spk-onnx>] [--notes "..."] [--channel beta|stable]
//
// Each build goes to site/v/<version>-<build>/ and is never overwritten: older builds stay available to
// testers (like TestFlight). Bump BUILD (or APP_VERSION) in web/js/version.js before every build.
import fs from "node:fs";
import path from "node:path";
import { makeIcons } from "./icons.mjs";
import { execFileSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SITE = path.join(HERE, "site");
const NM = path.join(HERE, "node_modules");
const args = process.argv.slice(2);
const arg = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };

const { APP_VERSION, BUILD, CHANNEL, NOTES } = await import(pathToFileURL(path.join(HERE, "web/js/version.js")).href);
const ID = `${APP_VERSION}-${BUILD}`;
const channel = arg("--channel") || CHANNEL;
const log = (...a) => console.log(...a);

function copyDir(src, dst, skip = () => false) {
  fs.mkdirSync(dst, { recursive: true });
  for (const e of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, e.name), d = path.join(dst, e.name);
    if (skip(s)) continue;
    if (e.isDirectory()) copyDir(s, d, skip);
    else fs.copyFileSync(s, d);
  }
}

function listFiles(dir, base = dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? listFiles(path.join(dir, e.name), base) : [path.relative(base, path.join(dir, e.name)).replaceAll("\\", "/")]);
}

// ---------- 1. this build (never overwrite an older one) ----------
const buildDir = path.join(SITE, "v", ID);
if (fs.existsSync(buildDir) && !args.includes("--force")) {
  console.error(`Build ${ID} already exists (old builds are kept). Bump BUILD in web/js/version.js first.`);
  process.exit(1);
}
fs.rmSync(buildDir, { recursive: true, force: true });
copyDir(path.join(HERE, "web"), buildDir);
fs.writeFileSync(path.join(buildDir, "files.json"), JSON.stringify(["./", ...listFiles(buildDir)], null, 1));
log(`build   v/${ID}/`);

// ---------- 2. launcher, service worker, install page, icons ----------
copyDir(path.join(HERE, "root"), SITE);
fs.writeFileSync(path.join(SITE, ".nojekyll"), "");                // GitHub Pages: serve files as they are
fs.writeFileSync(path.join(SITE, "_headers"),                      // Netlify / Cloudflare Pages
  "/sw.js\n  Cache-Control: no-cache\n/releases.json\n  Cache-Control: no-cache\n/index.html\n  Cache-Control: no-cache\n" +
  "/*.gz\n  Content-Type: application/octet-stream\n");
makeIcons(path.join(SITE, "icons"));

// ---------- 3. libraries (versioned folders, shared by builds) ----------
const libs = {
  "vosk-0.0.8": [["vosk-browser/dist/vosk.js", "vosk.js"]],
  "transformers-3.7.5": [["@huggingface/transformers/dist/transformers.min.js", "transformers.min.js"],
    ["@huggingface/transformers/dist/ort-wasm-simd-threaded.jsep.mjs", "ort-wasm-simd-threaded.jsep.mjs"],
    ["@huggingface/transformers/dist/ort-wasm-simd-threaded.jsep.wasm", "ort-wasm-simd-threaded.jsep.wasm"]],
  "ort-1.22.0": [["onnxruntime-web/dist/ort.wasm.min.mjs", "ort.wasm.min.mjs"],
    ["onnxruntime-web/dist/ort-wasm-simd-threaded.mjs", "ort-wasm-simd-threaded.mjs"],
    ["onnxruntime-web/dist/ort-wasm-simd-threaded.wasm", "ort-wasm-simd-threaded.wasm"]],
  "qrcode-1.4.4": [["qrcode-generator/qrcode.js", "qrcode.js"]],
  // Learning: read PDFs (text, layout, form fields) and fill the journal template with a Vietnamese font
  "pdfjs-4.10.38": [["pdfjs-dist/legacy/build/pdf.min.mjs", "pdf.min.mjs"],
    ["pdfjs-dist/legacy/build/pdf.worker.min.mjs", "pdf.worker.min.mjs"]],
  "pdf-lib-1.17.1": [["pdf-lib/dist/pdf-lib.min.js", "pdf-lib.min.js"],
    ["@pdf-lib/fontkit/dist/fontkit.umd.min.js", "fontkit.umd.min.js"],
    ["dejavu-fonts-ttf/ttf/DejaVuSans.ttf", "DejaVuSans.ttf"],
    ["dejavu-fonts-ttf/ttf/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"]],
};
for (const [dir, files] of Object.entries(libs)) {
  for (const [src, name] of files) {
    const d = path.join(SITE, "lib", dir, name);
    if (fs.existsSync(d)) continue;
    fs.mkdirSync(path.dirname(d), { recursive: true });
    fs.copyFileSync(path.join(NM, src), d);
  }
}
log("libs    ok");

// ---------- 4. models ----------
const modelsSrc = [arg("--models"), path.join(HERE, "..", "models"), path.join(HERE, "..", "..", "translate", "models")]
  .find((p) => p && fs.existsSync(path.join(p, "vosk-en-small")));
const M = path.join(SITE, "models");
fs.mkdirSync(path.join(M, "spk"), { recursive: true });
const spk = path.join(M, "spk", "campplus.onnx");
const vosk = path.join(M, "vosk-en-small.tar.gz");
if ((!fs.existsSync(spk) || !fs.existsSync(vosk)) && !modelsSrc) {
  console.error("Model folder not found: pass --models <folder containing vosk-en-small and spk-onnx>");
  process.exit(1);
}
if (!fs.existsSync(spk)) {
  fs.copyFileSync(path.join(modelsSrc, "spk-onnx", "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"), spk);
}
if (!fs.existsSync(vosk)) {
  // vosk-browser unpacks a .tar.gz whose files sit in one top folder
  const tar = process.platform === "win32" && fs.existsSync("C:/Windows/System32/tar.exe") ? "C:/Windows/System32/tar.exe" : "tar";
  execFileSync(tar, ["-czf", vosk.replaceAll("\\", "/"), "-C", modelsSrc.replaceAll("\\", "/"), "vosk-en-small"], { stdio: "inherit" });
}
const W = path.join(M, "whisper-base.en");
const HF = "https://huggingface.co/onnx-community/whisper-base.en/resolve/main/";
const whisperFiles = ["config.json", "generation_config.json", "preprocessor_config.json", "tokenizer.json",
  "tokenizer_config.json", "added_tokens.json", "special_tokens_map.json", "normalizer.json", "vocab.json", "merges.txt",
  "onnx/encoder_model_quantized.onnx", "onnx/decoder_model_merged_quantized.onnx", "onnx/encoder_model.onnx"];
for (const f of whisperFiles) {
  const d = path.join(W, f);
  if (fs.existsSync(d)) continue;
  fs.mkdirSync(path.dirname(d), { recursive: true });
  log(`download whisper-base.en/${f}`);
  const res = await fetch(HF + f);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${f}`);
  fs.writeFileSync(d + ".part", Buffer.from(await res.arrayBuffer()));
  fs.renameSync(d + ".part", d);
}
log("models  ok");

// ---------- 5. releases.json (the list testers see) ----------
const relSrc = path.join(HERE, "releases.json");
const rel = fs.existsSync(relSrc) ? JSON.parse(fs.readFileSync(relSrc, "utf8")) : { app: "Live Translator iPad", feedback_email: "", releases: [] };
const notes = arg("--notes") ? arg("--notes").split("|") : NOTES;
const d = new Date();
const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
rel.releases = rel.releases.filter((r) => `${r.version}-${r.build}` !== ID);
rel.releases.push({ version: APP_VERSION, build: BUILD, channel, date: today, notes });
// a build whose folder is missing on this machine is dropped from the list
rel.releases = rel.releases.filter((r) => fs.existsSync(path.join(SITE, "v", `${r.version}-${r.build}`)));
fs.writeFileSync(relSrc, JSON.stringify(rel, null, 2) + "\n");
fs.copyFileSync(relSrc, path.join(SITE, "releases.json"));
log(`release ${ID} (${channel}) — ${rel.releases.length} build(s) online`);

