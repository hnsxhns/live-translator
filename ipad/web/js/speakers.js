// Groups utterances by voice (CAM++ voiceprints), online. Same algorithm and thresholds as the desktop app.
//
// Each utterance's voiceprint is compared with the average voiceprint of every speaker heard so far;
// close enough -> same person, otherwise a new speaker. Short utterances never create a new speaker.
// When two groups later turn out to be the same voice they are merged (`merged`: old id -> kept id).

const DIM = 512;

function unit(v) {
  let n = 0;
  for (let i = 0; i < v.length; i++) n += v[i] * v[i];
  n = Math.sqrt(n) || 1;
  return v.map((x) => x / n);
}

function dot(a, b) {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

export class SpeakerTracker {
  static SAME = 0.36;         // similarity to a speaker's average voice to count as that speaker
  static MERGE = 0.50;        // two speakers' average voices this similar -> the same person
  static MIN_SECONDS = 1.2;   // shorter utterances are too short to start a new speaker
  static SMALL = 8.0;         // a "speaker" with less talk than this (seconds) is still uncertain
  static MERGE_SMALL = 0.50;

  constructor() {
    this.sums = new Map();    // speaker id -> weighted sum of voiceprints (Float32Array)
    this.talk = {};           // speaker id -> seconds spoken
    this.merged = {};         // old speaker id -> speaker id it was merged into
    this.last = null;
    this.nextId = 1;
  }

  resolve(sid) {
    let n = 0;
    while (sid in this.merged && n++ < 100) sid = this.merged[sid];
    return sid;
  }

  assign(vec, seconds) {
    const S = SpeakerTracker;
    let sid = this.last ? this.resolve(this.last) : null;
    if (vec && vec.length) {
      const v = Float32Array.from(vec);
      let best = null, bestSim = -1;
      for (const [s, total] of this.sums) {
        const sim = dot(v, unit(total));
        if (sim > bestSim) { best = s; bestSim = sim; }
      }
      if (best !== null && (bestSim >= S.SAME || seconds < S.MIN_SECONDS)) {
        sid = best;
      } else if (best === null || seconds >= S.MIN_SECONDS) {
        sid = this.nextId++;
        this.sums.set(sid, new Float32Array(v.length));
      }
      const w = Math.min(seconds, 6.0);                     // longer speech counts more
      const sum = this.sums.get(sid);
      for (let i = 0; i < v.length; i++) sum[i] += v[i] * w;
      this.mergeSimilar();
      sid = this.resolve(sid);
    }
    if (sid === null) {                                     // no voiceprint at all
      sid = this.nextId++;
      this.sums.set(sid, new Float32Array(DIM));
    }
    this.talk[sid] = (this.talk[sid] || 0) + seconds;
    this.last = sid;
    return sid;
  }

  mergeSimilar() {
    const S = SpeakerTracker;
    for (;;) {
      const ids = [...this.sums.keys()].sort((a, b) => a - b);
      const units = new Map(ids.map((s) => [s, unit(this.sums.get(s))]));
      let pair = null, best = -1;
      for (let i = 0; i < ids.length; i++) {
        for (let j = i + 1; j < ids.length; j++) {
          const a = ids[i], b = ids[j];
          const sim = dot(units.get(a), units.get(b));
          const small = Math.min(this.talk[a] || 0, this.talk[b] || 0) < S.SMALL;
          const need = small ? S.MERGE_SMALL : S.MERGE;
          if (sim >= need && (pair === null || sim > best)) { pair = [a, b]; best = sim; }
        }
      }
      if (!pair) return;
      const [a, b] = pair;
      const [keep, gone] = (this.talk[a] || 0) >= (this.talk[b] || 0) ? [a, b] : [b, a];
      const ks = this.sums.get(keep), gs = this.sums.get(gone);
      for (let i = 0; i < ks.length; i++) ks[i] += gs[i];
      this.sums.delete(gone);
      this.talk[keep] = (this.talk[keep] || 0) + (this.talk[gone] || 0);
      delete this.talk[gone];
      this.merged[gone] = keep;
    }
  }
}
