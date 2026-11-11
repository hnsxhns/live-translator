// Whisper base.en (the same model as the desktop app) re-transcribes each finished utterance for
// accurate, punctuated text. Uses the iPad GPU (WebGPU) for the heavy encoder when available.
import { pipeline, env } from "../../../lib/transformers-3.7.5/transformers.min.js";

const ROOT = new URL("../../../", import.meta.url).href;
const MODEL = "whisper-base.en";
let asr = null;

async function load(gpu, native) {
  env.allowRemoteModels = false;
  env.allowLocalModels = true;
  env.localModelPath = ROOT + "models/";
  env.useBrowserCache = false;
  // web: files were downloaded into the "lt-models" cache by the app; TestFlight app: they ship inside it
  env.useCustomCache = !native;
  if (!native) env.customCache = await caches.open("lt-models");
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

/** Token ids of a special token such as "<|startofprev|>" (undefined if the model has none). */
function special(tok, t) {
  const v = tok.model?.tokens_to_ids?.get?.(t);
  if (v !== undefined) return v;
  const ids = tok.encode(t, { add_special_tokens: false });
  return ids.length === 1 ? ids[0] : undefined;
}

/** Whisper with a prompt (the course's vocabulary + the last words heard), like initial_prompt on the desktop:
 *  <|startofprev|> prompt <|startoftranscript|> <|notimestamps|> -> the words of this audio. */
async function transcribePrompted(a, prompt) {
  const tok = asr.tokenizer, model = asr.model;
  const prev = special(tok, "<|startofprev|>"), sot = special(tok, "<|startoftranscript|>"),
        nots = special(tok, "<|notimestamps|>");
  if (prev === undefined || sot === undefined || nots === undefined) return null;
  const ptoks = tok.encode(" " + prompt, { add_special_tokens: false }).slice(-160);   // Whisper reads <= 223
  const ids = [prev, ...ptoks, sot, nots];
  const { input_features } = await asr.processor(a);
  const out = await model.generate({ inputs: input_features, decoder_input_ids: ids, max_new_tokens: 160 });
  const seq = Array.from(out.tolist ? out.tolist()[0] : out[0], Number).slice(ids.length);
  return tok.decode(seq, { skip_special_tokens: true }).trim();
}

async function transcribe(audio, prompt = "") {
  let peak = 0;
  for (const v of audio) peak = Math.max(peak, Math.abs(v));
  const gain = Math.min(8.0, 0.6 / (peak || 1));           // auto-gain: quiet mics are boosted (max 8x)
  const a = audio.map((v) => v * gain);
  if (prompt) {
    try {
      const t = await transcribePrompted(a, prompt);
      if (t !== null) return t;
    } catch (e) {
      console.warn("whisper prompt off:", e);          // e.g. an older model: plain transcription below
    }
  }
  const out = await asr(a, { return_timestamps: false });
  return (out?.text || "").trim();
}

let loading = null;
self.onmessage = async (ev) => {
  const msg = ev.data;
  try {
    if (msg.type === "load") {
      loading = loading || load(msg.gpu, msg.native);
      const mode = await loading;
      self.postMessage({ type: "ready", mode });
    } else if (msg.type === "run") {
      await loading;
      const text = await transcribe(msg.audio, String(msg.prompt || "").trim());
      self.postMessage({ type: "text", id: msg.id, text });
    }
  } catch (e) {
    self.postMessage({ type: "error", id: msg.id, error: String(e?.message || e) });
  }
};
