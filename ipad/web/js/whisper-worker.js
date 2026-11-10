// Whisper base.en (the same model as the desktop app) re-transcribes each finished utterance for
// accurate, punctuated text. Uses the iPad GPU (WebGPU) for the heavy encoder when available.
import { pipeline, env } from "../../../lib/transformers-3.7.5/transformers.min.js";

const ROOT = new URL("../../../", import.meta.url).href;
const MODEL = "whisper-base.en";
let asr = null;

async function load(gpu) {
  env.allowRemoteModels = false;
  env.allowLocalModels = true;
  env.localModelPath = ROOT + "models/";
  env.useBrowserCache = false;
  env.useCustomCache = true;
  env.customCache = await caches.open("lt-models");     // files were downloaded there by the app
  env.backends.onnx.wasm.wasmPaths = ROOT + "lib/transformers-3.7.5/";
  env.backends.onnx.wasm.numThreads = 1;
  const tries = gpu
    ? [{ device: { encoder_model: "webgpu", decoder_model_merged: "wasm" },
         dtype: { encoder_model: "fp32", decoder_model_merged: "q8" } },
       { device: "wasm", dtype: "q8" }]
    : [{ device: "wasm", dtype: "q8" }];
  let last;
  for (const opts of tries) {
    try {
      asr = await pipeline("automatic-speech-recognition", MODEL, opts);
      await asr(new Float32Array(8000));                     // warm-up
      return opts.device === "wasm" ? "cpu" : "gpu";
    } catch (e) {
      last = e;
      asr = null;
    }
  }
  throw last;
}

async function transcribe(audio) {
  let peak = 0;
  for (const v of audio) peak = Math.max(peak, Math.abs(v));
  const gain = Math.min(8.0, 0.6 / (peak || 1));           // auto-gain: quiet mics are boosted (max 8x)
  const a = audio.map((v) => v * gain);
  const out = await asr(a, { return_timestamps: false });
  return (out?.text || "").trim();
}

let loading = null;
self.onmessage = async (ev) => {
  const msg = ev.data;
  try {
    if (msg.type === "load") {
      loading = loading || load(msg.gpu);
      const mode = await loading;
      self.postMessage({ type: "ready", mode });
    } else if (msg.type === "run") {
      await loading;
      const text = await transcribe(msg.audio);
      self.postMessage({ type: "text", id: msg.id, text });
    }
  } catch (e) {
    self.postMessage({ type: "error", id: msg.id, error: String(e?.message || e) });
  }
};
