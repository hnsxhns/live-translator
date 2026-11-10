// Kaldi-style 80-bin log-mel filterbank, matching what sherpa-onnx feeds the CAM++ voiceprint model
// on the desktop app (25 ms frames, 10 ms hop, povey window, no edge snipping, mean-normalized).
// Pure JS so it runs in a worker on the iPad and in Node for the parity test.

const SR = 16000;
const FRAME_LEN = 400;          // 25 ms
const SHIFT = 160;              // 10 ms
const NFFT = 512;
const BINS = 80;
const LOW = 20;
const HIGH = SR / 2 - 400;      // 7600 Hz
const PREEMPH = 0.97;
const EPS = 1.1920928955078125e-7;

const mel = (f) => 1127 * Math.log(1 + f / 700);

const WINDOW = new Float32Array(FRAME_LEN).map((_, i) =>
  Math.pow(0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (FRAME_LEN - 1)), 0.85));

// mel filters: for each bin, [first fft index, weights...]
const FILTERS = (() => {
  const lo = mel(LOW), hi = mel(HIGH), delta = (hi - lo) / (BINS + 1);
  const width = SR / NFFT;
  const out = [];
  for (let b = 0; b < BINS; b++) {
    const left = lo + b * delta, center = left + delta, right = center + delta;
    let first = -1;
    const w = [];
    for (let i = 0; i < NFFT / 2; i++) {
      const m = mel(width * i);
      if (m > left && m < right) {
        if (first < 0) first = i;
        w.push(m <= center ? (m - left) / (center - left) : (right - m) / (right - center));
      }
    }
    out.push([first, Float32Array.from(w)]);
  }
  return out;
})();

// in-place radix-2 complex FFT
const REV = (() => {
  const r = new Uint16Array(NFFT);
  const bits = Math.log2(NFFT);
  for (let i = 0; i < NFFT; i++) {
    let x = i, y = 0;
    for (let b = 0; b < bits; b++) { y = (y << 1) | (x & 1); x >>= 1; }
    r[i] = y;
  }
  return r;
})();
const COS = new Float64Array(NFFT / 2).map((_, i) => Math.cos((-2 * Math.PI * i) / NFFT));
const SIN = new Float64Array(NFFT / 2).map((_, i) => Math.sin((-2 * Math.PI * i) / NFFT));

function fft(re, im) {
  for (let i = 0; i < NFFT; i++) {
    const j = REV[i];
    if (j > i) {
      let t = re[i]; re[i] = re[j]; re[j] = t;
      t = im[i]; im[i] = im[j]; im[j] = t;
    }
  }
  for (let size = 2; size <= NFFT; size <<= 1) {
    const half = size >> 1, step = NFFT / size;
    for (let s = 0; s < NFFT; s += size) {
      for (let k = 0; k < half; k++) {
        const c = COS[k * step], d = SIN[k * step];
        const a = s + k, b = a + half;
        const tr = re[b] * c - im[b] * d, ti = re[b] * d + im[b] * c;
        re[b] = re[a] - tr; im[b] = im[a] - ti;
        re[a] += tr; im[a] += ti;
      }
    }
  }
}

/** audio: Float32Array at 16 kHz in [-1, 1]. Returns {data: Float32Array(frames*80), frames}. */
export function fbank(audio) {
  const n = audio.length;
  const frames = Math.floor((n + SHIFT / 2) / SHIFT);
  const out = new Float32Array(frames * BINS);
  const buf = new Float64Array(FRAME_LEN);
  const re = new Float64Array(NFFT), im = new Float64Array(NFFT);
  for (let f = 0; f < frames; f++) {
    // snip_edges = false: frame centered on f*shift + shift/2, edges mirrored
    const start = f * SHIFT + SHIFT / 2 - FRAME_LEN / 2;
    let mean = 0;
    for (let i = 0; i < FRAME_LEN; i++) {
      let s = start + i;
      if (s < 0) s = -s - 1;
      else if (s >= n) s = 2 * n - 1 - s;
      buf[i] = audio[Math.min(n - 1, Math.max(0, s))];      // sherpa keeps samples in [-1, 1]
      mean += buf[i];
    }
    mean /= FRAME_LEN;
    for (let i = 0; i < FRAME_LEN; i++) buf[i] -= mean;
    for (let i = FRAME_LEN - 1; i > 0; i--) buf[i] -= PREEMPH * buf[i - 1];
    buf[0] -= PREEMPH * buf[0];
    re.fill(0); im.fill(0);
    for (let i = 0; i < FRAME_LEN; i++) re[i] = buf[i] * WINDOW[i];
    fft(re, im);
    for (let b = 0; b < BINS; b++) {
      const [first, w] = FILTERS[b];
      let e = 0;
      for (let k = 0; k < w.length; k++) {
        const i = first + k;
        e += w[k] * (re[i] * re[i] + im[i] * im[i]);
      }
      out[f * BINS + b] = Math.log(Math.max(e, EPS));
    }
  }
  // mean normalization over time (per bin)
  for (let b = 0; b < BINS; b++) {
    let m = 0;
    for (let f = 0; f < frames; f++) m += out[f * BINS + b];
    m /= frames || 1;
    for (let f = 0; f < frames; f++) out[f * BINS + b] -= m;
  }
  return { data: out, frames };
}

export const FBANK_BINS = BINS;
