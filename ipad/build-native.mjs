// Prepares the files the native iPad app (ipad/ios, TestFlight) carries inside it:
//   ios/site/    the web app (current version), its libraries and all speech models
//   ios/LiveTranslator/Assets.xcassets/AppIcon.appiconset/icon-1024.png
//
//   node build-native.mjs [--models-url <published site>]
//
// Models come from ipad/site/models (made by build.mjs) when present, otherwise they are downloaded
// from the published site (default: the GitHub Pages copy) and Hugging Face — that is what CI does.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { png, iconPixel } from "./icons.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(HERE, "ios", "site");
const NM = path.join(HERE, "node_modules");
const args = process.argv.slice(2);
const at = args.indexOf("--models-url");
const MODELS_URL = (at >= 0 ? args[at + 1] : "https://hnsxhns.github.io/live-translator-ipad/").replace(/\/?$/, "/");

const { APP_VERSION, BUILD, NOTES } = await import(pathToFileURL(path.join(HERE, "web/js/version.js")).href);
const ID = `${APP_VERSION}-${BUILD}`;

function copyDir(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const e of fs.readdirSync(src, { withFileTypes: true })) {
    if (e.name.startsWith(".")) continue;
    const s = path.join(src, e.name), d = path.join(dst, e.name);
    if (e.isDirectory()) copyDir(s, d);
    else fs.copyFileSync(s, d);
  }
}

fs.rmSync(OUT, { recursive: true, force: true });
copyDir(path.join(HERE, "web"), path.join(OUT, "v", ID));
for (const f of ["index.html", "manifest.webmanifest"]) fs.copyFileSync(path.join(HERE, "root", f), path.join(OUT, f));
fs.mkdirSync(path.join(OUT, "icons"), { recursive: true });
for (const s of [180, 192, 512]) fs.writeFileSync(path.join(OUT, "icons", `icon-${s}.png`), png(s, iconPixel));
// only the build inside the app: the launcher opens it, updates come through TestFlight
fs.writeFileSync(path.join(OUT, "releases.json"), JSON.stringify({ app: "Live Translator iPad", releases: [
  { version: APP_VERSION, build: BUILD, channel: "stable", date: "", notes: NOTES }] }, null, 1));

const libs = {
  "vosk-0.0.8": [["vosk-browser/dist/vosk.js", "vosk.js"]],
  "transformers-3.7.5": [["@huggingface/transformers/dist/transformers.min.js", "transformers.min.js"],
    ["@huggingface/transformers/dist/ort-wasm-simd-threaded.jsep.mjs", "ort-wasm-simd-threaded.jsep.mjs"],
    ["@huggingface/transformers/dist/ort-wasm-simd-threaded.jsep.wasm", "ort-wasm-simd-threaded.jsep.wasm"]],
  "ort-1.22.0": [["onnxruntime-web/dist/ort.wasm.min.mjs", "ort.wasm.min.mjs"],
    ["onnxruntime-web/dist/ort-wasm-simd-threaded.mjs", "ort-wasm-simd-threaded.mjs"],
    ["onnxruntime-web/dist/ort-wasm-simd-threaded.wasm", "ort-wasm-simd-threaded.wasm"]],
  "pdfjs-4.10.38": [["pdfjs-dist/legacy/build/pdf.min.mjs", "pdf.min.mjs"],
    ["pdfjs-dist/legacy/build/pdf.worker.min.mjs", "pdf.worker.min.mjs"]],
  "pdf-lib-1.17.1": [["pdf-lib/dist/pdf-lib.min.js", "pdf-lib.min.js"],
    ["@pdf-lib/fontkit/dist/fontkit.umd.min.js", "fontkit.umd.min.js"],
    ["dejavu-fonts-ttf/ttf/DejaVuSans.ttf", "DejaVuSans.ttf"]],
};
for (const [dir, files] of Object.entries(libs)) {
  fs.mkdirSync(path.join(OUT, "lib", dir), { recursive: true });
  for (const [src, name] of files) fs.copyFileSync(path.join(NM, src), path.join(OUT, "lib", dir, name));
}

const W = "models/whisper-base.en/";
const models = ["models/vosk-en-small.tar.gz", "models/spk/campplus.onnx",
  ...["config.json", "generation_config.json", "preprocessor_config.json", "tokenizer.json", "tokenizer_config.json",
      "added_tokens.json", "special_tokens_map.json", "normalizer.json", "vocab.json", "merges.txt",
      "onnx/encoder_model_quantized.onnx", "onnx/decoder_model_merged_quantized.onnx", "onnx/encoder_model.onnx"]
    .map((f) => W + f)];
for (const m of models) {
  const dst = path.join(OUT, m);
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  const local = path.join(HERE, "site", m);
  if (fs.existsSync(local)) { fs.copyFileSync(local, dst); continue; }
  const url = MODELS_URL + m;
  console.log("download", url);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  fs.writeFileSync(dst, Buffer.from(await res.arrayBuffer()));
}

const icon = path.join(HERE, "ios", "LiveTranslator", "Assets.xcassets", "AppIcon.appiconset", "icon-1024.png");
fs.mkdirSync(path.dirname(icon), { recursive: true });
fs.writeFileSync(icon, png(1024, iconPixel));

const size = (d) => fs.readdirSync(d, { withFileTypes: true })
  .reduce((n, e) => n + (e.isDirectory() ? size(path.join(d, e.name)) : fs.statSync(path.join(d, e.name)).size), 0);
console.log(`native site ${ID}: ${(size(OUT) / 1e6).toFixed(0)} MB in ios/site`);
