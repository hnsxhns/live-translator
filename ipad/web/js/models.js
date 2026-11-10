// Model files: downloaded once (with progress) into the "lt-models" cache, then used offline.

export const ROOT = new URL("../../../", import.meta.url).href;
const WHISPER = "models/whisper-base.en/";
const WHISPER_SMALL = ["config.json", "generation_config.json", "preprocessor_config.json", "tokenizer.json",
                       "tokenizer_config.json", "added_tokens.json", "special_tokens_map.json", "normalizer.json",
                       "vocab.json", "merges.txt"];

export const VOSK_URL = ROOT + "models/vosk-en-small.tar.gz";

export async function hasWebGPU() {
  try {
    return !!(navigator.gpu && await navigator.gpu.requestAdapter());
  } catch {
    return false;
  }
}

export function modelFiles(gpu, whisper = true) {
  const files = ["models/vosk-en-small.tar.gz", "models/spk/campplus.onnx"];
  if (whisper) {
    files.push(...WHISPER_SMALL.map((f) => WHISPER + f),
               WHISPER + "onnx/encoder_model_quantized.onnx", WHISPER + "onnx/decoder_model_merged_quantized.onnx");
    if (gpu) files.push(WHISPER + "onnx/encoder_model.onnx");
  }
  return files.map((f) => ROOT + f);
}

/** Download every missing file into the cache. onProgress(doneBytes, totalBytes) */
export async function ensureFiles(urls, onProgress) {
  const cache = await caches.open("lt-models");
  const missing = [];
  for (const u of urls) if (!(await cache.match(u))) missing.push(u);
  if (!missing.length) return false;
  try { await navigator.storage?.persist?.(); } catch { /* not supported */ }
  const sizes = await Promise.all(missing.map(async (u) => {
    try {
      const r = await fetch(u, { method: "HEAD", cache: "no-store" });
      return +r.headers.get("content-length") || 0;
    } catch { return 0; }
  }));
  const total = sizes.reduce((a, b) => a + b, 0);
  let done = 0;
  for (const u of missing) {
    const res = await fetch(u, { cache: "no-store" });
    if (!res.ok) throw new Error(`Không tải được ${u.slice(ROOT.length)} (HTTP ${res.status})`);
    const reader = res.body.getReader();
    const parts = [];
    for (;;) {
      const { value, done: end } = await reader.read();
      if (end) break;
      parts.push(value);
      done += value.length;
      onProgress?.(done, total);
    }
    const type = res.headers.get("content-type") || "application/octet-stream";
    await cache.put(u, new Response(new Blob(parts, { type }), { headers: { "content-type": type } }));
  }
  return true;
}
