// Voiceprints (who is speaking): CAM++ model via onnxruntime-web, same input features as the desktop app.
import * as ort from "../../../lib/ort-1.22.0/ort.wasm.min.mjs";
import { fbank, FBANK_BINS } from "./fbank.js";

const ROOT = new URL("../../../", import.meta.url).href;
const SR = 16000;
let session = null;

async function getBytes(url) {
  const hit = await caches.match(url).catch(() => undefined);
  const res = hit || await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`);
  return new Uint8Array(await res.arrayBuffer());
}

async function load() {
  ort.env.wasm.wasmPaths = ROOT + "lib/ort-1.22.0/";
  ort.env.wasm.numThreads = 1;
  session = await ort.InferenceSession.create(await getBytes(ROOT + "models/spk/campplus.onnx"),
                                              { executionProviders: ["wasm"] });
}

function unit(v) {
  let n = 0;
  for (const x of v) n += x * x;
  n = Math.sqrt(n) || 1;
  return v.map((x) => x / n);
}

async function windowEmbed(x) {
  const { data, frames } = fbank(x);
  const out = await session.run({ [session.inputNames[0]]: new ort.Tensor("float32", data, [1, frames, FBANK_BINS]) });
  return unit(Float32Array.from(out[session.outputNames[0]].data));
}

// The model gives reliable voiceprints only for exactly-2-second inputs: 2 s windows, 1 s apart,
// averaged (at most 8). Short audio is looped up to 2 s.
async function embed(audio) {
  const W = 2 * SR;
  let x = audio;
  if (x.length < W) {
    const t = new Float32Array(W);
    for (let i = 0; i < W; i++) t[i] = x[i % Math.max(1, x.length)];
    x = t;
  }
  const vs = [];
  for (let i = 0; i + W <= x.length && vs.length < 8; i += SR) vs.push(await windowEmbed(x.subarray(i, i + W)));
  const m = new Float32Array(vs[0].length);
  for (const v of vs) for (let i = 0; i < m.length; i++) m[i] += v[i] / vs.length;
  return unit(m);
}

let ready = null;
self.onmessage = async (ev) => {
  const msg = ev.data;
  try {
    if (msg.type === "load") {
      ready = ready || load();
      await ready;
      self.postMessage({ type: "ready" });
    } else if (msg.type === "embed") {
      await ready;
      const vec = await embed(msg.audio);
      self.postMessage({ type: "vec", id: msg.id, vec }, [vec.buffer]);
    }
  } catch (e) {
    self.postMessage({ type: "error", id: msg.id, error: String(e?.message || e) });
  }
};
