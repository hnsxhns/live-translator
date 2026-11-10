// Microphone -> 16 kHz mono chunks of 100 ms (runs on the audio thread).
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;     // e.g. 3 on iPad (48 kHz)
    this.pos = 0;                        // fractional read position in `acc`
    this.acc = [];                       // input samples not yet consumed
    this.out = new Float32Array(1600);
    this.n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) this.acc.push(ch[i]);
    // box filter over each output period (simple low-pass) then decimate
    while (this.pos + this.ratio <= this.acc.length) {
      const a = Math.floor(this.pos), b = Math.max(a + 1, Math.floor(this.pos + this.ratio));
      let s = 0;
      for (let k = a; k < b; k++) s += this.acc[k];
      this.out[this.n++] = s / (b - a);
      this.pos += this.ratio;
      if (this.n === this.out.length) {
        this.port.postMessage(this.out, [this.out.buffer]);
        this.out = new Float32Array(1600);
        this.n = 0;
      }
    }
    const drop = Math.floor(this.pos);
    if (drop > 0) {
      this.acc.splice(0, drop);
      this.pos -= drop;
    }
    return true;
  }
}
registerProcessor("lt-capture", Capture);
