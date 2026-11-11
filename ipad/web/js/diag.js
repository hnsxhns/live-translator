// 🩺 Mic check: measures every link from the iPad microphone to the English text, one by one, and says
// which one is broken. The result can be copied and sent (it has no key, no personal data).
//
// The order copies what the app does when ON is tapped (an AudioContext made during the tap, then the
// mic), so the problems seen while listening show up here too; when a link fails, the other ways the app
// can capture audio are tried as well, to show which one works on this iPad.

import { SR, downsampler } from "./recognizer.js";
import { APP_VERSION, BUILD } from "./version.js";

const el = (tag, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
/** [finished in time?, value or error, ms] — a step never hangs the check. */
async function timed(p, ms) {
  const t0 = performance.now();
  let done = false, val, err;
  await Promise.race([
    Promise.resolve(p).then((v) => { done = true; val = v; }, (e) => { done = true; err = e; }),
    sleep(ms),
  ]);
  return { done, val, err, ms: Math.round(performance.now() - t0) };
}
const errText = (e) => (e ? `${e.name || "Error"}: ${e.message || e}` : "");

export class MicCheck {
  constructor(app) {
    this.app = app;
  }

  open() {
    const body = el("div", "lform diag");
    body.append(el("p", "muted",
      "Kiểm tra từng bước từ micro của iPad đến chữ tiếng Anh, rồi cho biết bước nào bị lỗi. " +
      "Khi được nhắc, hãy nói một câu tiếng Anh bình thường (ví dụ: \"Hello, this is a microphone test\")."));
    this.say = el("div", "diagSay", "");
    this.say.hidden = true;
    this.meter = el("div", "diagMeter");
    this.meterBar = el("i");
    this.meter.append(this.meterBar);
    this.meter.hidden = true;
    this.list = el("div", "diagList");
    this.verdict = el("div", "diagVerdict");
    this.verdict.hidden = true;
    const row = el("div", "actions");
    this.startBtn = el("button", "primary", "▶ Bắt đầu kiểm tra");
    this.copyBtn = el("button", "", "📋 Copy kết quả");
    this.copyBtn.hidden = true;
    const close = el("button", "", "Đóng");
    close.onclick = () => this.app.closeSheet();
    // the tap itself makes the AudioContext, exactly as the ON button does
    this.startBtn.onclick = () => {
      const ctx = new AudioContext();
      const resumeP = ctx.resume();
      this.run(ctx, resumeP);
    };
    this.copyBtn.onclick = () => this.copy();
    row.append(this.copyBtn, el("span", "spacer"), close, this.startBtn);
    body.append(this.say, this.meter, this.list, this.verdict, row);
    this.app.sheet("🩺 Kiểm tra micro", body, true);
  }

  step(title) {
    const r = el("div", "diagStep run");
    const icon = el("span", "dIcon", "⏳");
    const main = el("div", "dMain");
    const detail = el("div", "dDetail", "");
    main.append(el("div", "dTitle", title), detail);
    r.append(icon, main);
    this.list.append(r);
    r.scrollIntoView({ block: "nearest" });
    const set = (kind, text) => {
      r.className = "diagStep " + kind;
      icon.textContent = { ok: "✓", bad: "✗", warn: "!", info: "•", run: "⏳" }[kind];
      detail.textContent = text;
      this.report.push(`${{ ok: "[OK]", bad: "[LỖI]", warn: "[CHÚ Ý]", info: "[--]", run: "[..]" }[kind]} ${title}: ${text}`);
    };
    return { set, note: (text) => { detail.textContent = text; } };
  }

  level(rms) {
    const db = rms > 0 ? 20 * Math.log10(rms) : -100;
    this.meterBar.style.width = `${Math.max(0, Math.min(100, (db + 70) * 1.6))}%`;
  }

  /** Capture `secs` seconds with one way (ctx + "worklet" | "script"); returns what came in. */
  async capture(ctx, stream, how, secs, label) {
    const out = { blocks: 0, peak: 0, sumSq: 0, n: 0, zeros: 0, audio: [], error: "" };
    let src, node, mute;
    const onBlock = (b) => {
      out.blocks += 1;
      let sq = 0;
      for (let i = 0; i < b.length; i++) {
        const v = b[i];
        sq += v * v;
        if (v === 0) out.zeros += 1;
        const a = v < 0 ? -v : v;
        if (a > out.peak) out.peak = a;
      }
      out.sumSq += sq;
      out.n += b.length;
      out.audio.push(Float32Array.from(b));
      this.level(Math.sqrt(sq / (b.length || 1)));
    };
    try {
      src = ctx.createMediaStreamSource(stream);
      mute = ctx.createGain();
      mute.gain.value = 0;
      if (how === "worklet") {
        const add = await timed(ctx.audioWorklet.addModule(new URL("capture-worklet.js", import.meta.url)), 5000);
        if (!add.done || add.err) throw new Error(add.err ? errText(add.err) : "nạp AudioWorklet quá 5 giây");
        node = new AudioWorkletNode(ctx, "lt-capture");
        node.port.onmessage = (ev) => onBlock(ev.data);
      } else {
        node = ctx.createScriptProcessor(4096, 1, 1);
        const down = downsampler(ctx.sampleRate, onBlock);
        node.onaudioprocess = (ev) => down(ev.inputBuffer.getChannelData(0));
      }
      src.connect(node);
      node.connect(mute).connect(ctx.destination);
      this.say.hidden = false;
      this.meter.hidden = false;
      for (let s = secs; s > 0; s--) {
        this.say.textContent = `🗣 ${label} — hãy nói tiếng Anh ngay bây giờ... (${s})`;
        await sleep(1000);
      }
    } catch (e) {
      out.error = errText(e);
    } finally {
      try { if (node?.port) node.port.onmessage = null; if (node) node.onaudioprocess = null; } catch { /* */ }
      for (const x of [src, node, mute]) { try { x?.disconnect(); } catch { /* */ } }
      this.say.hidden = true;
      this.level(0);
    }
    const len = out.audio.reduce((a, b) => a + b.length, 0);
    const all = new Float32Array(len);
    let o = 0;
    for (const b of out.audio) { all.set(b, o); o += b.length; }
    out.audio = all;
    out.rms = out.n ? Math.sqrt(out.sumSq / out.n) : 0;
    out.zeroPct = out.n ? Math.round((100 * out.zeros) / out.n) : 0;
    out.expected = Math.round(secs * 10);           // 100 ms blocks
    out.good = out.blocks > 0 && out.peak > 0.003;
    return out;
  }

  describe(c) {
    if (c.error) return `lỗi: ${c.error}`;
    const db = c.rms > 0 ? (20 * Math.log10(c.rms)).toFixed(0) : "-∞";
    return `${c.blocks}/${c.expected} khối âm thanh · đỉnh ${c.peak.toFixed(3)} · mức TB ${db} dB · ${c.zeroPct}% mẫu bằng 0`;
  }

  /** Vosk on the captured audio: the same model the app uses for the live words. */
  async vosk(audio) {
    const model = this.app.recognizer.vosk;
    if (!model) return { error: "mô hình Vosk chưa nạp xong" };
    const rec = new model.KaldiRecognizer(SR);
    const texts = [];
    let partial = "";
    rec.on("result", (m) => { if (m.result?.text) texts.push(m.result.text); });
    rec.on("partialresult", (m) => { partial = m.result?.partial || partial; });
    for (let i = 0; i < audio.length; i += 1600) rec.acceptWaveformFloat(audio.slice(i, i + 1600), SR);
    rec.retrieveFinalResult();
    for (let k = 0; k < 40 && texts.length === 0; k++) await sleep(150);
    await sleep(300);
    try { rec.remove(); } catch { /* */ }
    return { text: (texts.join(" ") || partial).trim() };
  }

  async run(tapCtx, tapResume) {
    const app = this.app;
    this.report = [];
    this.list.replaceChildren();
    this.verdict.hidden = true;
    this.copyBtn.hidden = true;
    this.startBtn.disabled = true;
    this.startBtn.textContent = "Đang kiểm tra...";
    const facts = {};
    let stream = null;
    const extra = [];
    try {
      // 1. the device
      let s = this.step("Thiết bị và trình duyệt");
      const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
      const ios = (/OS (\d+[_\d]*)/.exec(navigator.userAgent) || [])[1]?.replace(/_/g, ".") || "";
      const ver = (/Version\/([\d.]+)/.exec(navigator.userAgent) || [])[1] || "";
      s.set("info", `${APP_VERSION} (${BUILD}) · ${window.LT_NATIVE ? "app TestFlight" : standalone ? "app ở Màn hình chính" : "Safari"}` +
        ` · iOS ${ios || "?"} · Safari ${ver || "?"} · ${navigator.maxTouchPoints > 1 ? "màn cảm ứng" : "không cảm ứng"}` +
        ` · AudioWorklet ${typeof AudioWorkletNode !== "undefined" ? "có" : "KHÔNG"} · WebGPU ${"gpu" in navigator ? "có" : "không"}` +
        ` · HTTPS ${window.isSecureContext ? "có" : "KHÔNG"}`);

      // 2. speech models
      s = this.step("Mô hình nhận diện giọng nói");
      const R = app.recognizer;
      facts.ready = !!app.ready;
      facts.vosk = !!R.vosk;
      if (app.ready) s.set("ok", `sẵn sàng · Vosk có · Whisper ${R.whisperMode ? `có (${R.whisperMode === "gpu" ? "GPU" : "CPU"})` : "không"} · nhận người nói ${R.spk ? "có" : "không"}`);
      else s.set(R.vosk ? "warn" : "bad", `CHƯA sẵn sàng — app sẽ không nghe cho tới khi xong. Vosk ${R.vosk ? "đã nạp" : "chưa nạp"}, ` +
        `Whisper ${R.whisperMode || "chưa xong"}. Trạng thái: "${document.querySelector(".progress span")?.textContent || $status()}"`);

      // listening must stop: the check needs the mic
      if (app.on) {
        s = this.step("Tắt chế độ nghe để kiểm tra");
        app.toggleOnOff();
        await sleep(1200);
        s.set("info", "đã tắt (bật ON lại sau khi kiểm tra)");
      }

      // 3. permission
      s = this.step("Quyền dùng micro");
      try {
        const p = await navigator.permissions.query({ name: "microphone" });
        facts.perm = p.state;
        s.set(p.state === "denied" ? "warn" : "info", ({ granted: "đã cho phép", prompt: "sẽ hỏi khi mở micro", denied: "trình duyệt báo \"bị từ chối\" (bước mở micro bên dưới cho biết chắc)" }[p.state] || p.state));
      } catch {
        s.set("info", "trình duyệt không cho xem trước (sẽ biết khi mở micro)");
      }

      // 4. the AudioContext made by the tap (as ON does)
      s = this.step("Bộ âm thanh tạo khi chạm (như nút ON)");
      let r = await timed(tapResume, 2000);
      facts.tapState1 = tapCtx.state;
      s.set(tapCtx.state === "running" ? "ok" : "warn",
        `${tapCtx.sampleRate} Hz · trạng thái "${tapCtx.state}" · resume ${r.done ? `xong sau ${r.ms} ms` : "CHƯA XONG sau 2 giây"}`);

      // 5. the microphone
      s = this.step("Mở micro (getUserMedia)");
      r = await timed(navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true } }), 20000);
      if (!r.done || r.err) {
        facts.gum = r.err?.name || "timeout";
        s.set("bad", r.err ? errText(r.err) : "không trả lời sau 20 giây");
        throw new Error("stop");
      }
      stream = r.val;
      const track = stream.getAudioTracks()[0];
      const st = track?.getSettings?.() || {};
      facts.trackMuted = !!track?.muted;
      s.set(track && track.readyState === "live" && !track.muted ? "ok" : "bad",
        `mở sau ${r.ms} ms · "${track?.label || "micro"}" · ${track?.readyState}${track?.muted ? " · BỊ TẮT TIẾNG (muted)" : ""}` +
        `${st.sampleRate ? ` · ${st.sampleRate} Hz` : ""}${st.channelCount ? ` · ${st.channelCount} kênh` : ""}`);

      // 6. the tap context after the mic opened (this is where it can hang on iPad)
      s = this.step("Bộ âm thanh sau khi mở micro");
      const before = tapCtx.state;
      r = await timed(tapCtx.resume(), 3000);
      facts.tapState2 = tapCtx.state;
      facts.resumeHang = !r.done;
      s.set(tapCtx.state === "running" ? "ok" : "bad",
        `trước: "${before}" → resume ${r.done ? `xong sau ${r.ms} ms` : "BỊ TREO (quá 3 giây, bản 9 sẽ đứng ở đây mãi)"} → "${tapCtx.state}"`);

      // 7. capture the way the app does
      let best = null;
      s = this.step("Thu âm cách app dùng (bộ âm thanh lúc chạm + AudioWorklet)");
      if (tapCtx.state === "running" && typeof AudioWorkletNode !== "undefined") {
        const c = await this.capture(tapCtx, stream, "worklet", 4, "Lần 1");
        facts.cap1 = c.good;
        s.set(c.good ? "ok" : "bad", this.describe(c));
        if (c.good) best = c;
      } else {
        facts.cap1 = false;
        s.set("bad", "bỏ qua: bộ âm thanh không chạy");
      }

      // 8. other ways, only if that failed: a context made after the mic, then ScriptProcessor
      if (!best) {
        s = this.step("Cách khác: bộ âm thanh MỚI tạo sau khi mở micro + AudioWorklet");
        const ctx2 = new AudioContext();
        extra.push(ctx2);
        r = await timed(ctx2.resume(), 2000);
        if (ctx2.state === "running" && typeof AudioWorkletNode !== "undefined") {
          const c = await this.capture(ctx2, stream, "worklet", 3, "Lần 2");
          facts.cap2 = c.good;
          s.set(c.good ? "ok" : "bad", `${ctx2.sampleRate} Hz · ` + this.describe(c));
          if (c.good) best = c;
        } else {
          facts.cap2 = false;
          s.set("bad", `bộ âm thanh mới không chạy ("${ctx2.state}", resume ${r.done ? "xong" : "treo"})`);
        }
        if (!best) {
          s = this.step("Cách khác: ScriptProcessor (cách cũ)");
          const ctx3 = ctx2.state === "running" ? ctx2 : tapCtx;
          if (ctx3.state === "running") {
            const c = await this.capture(ctx3, stream, "script", 3, "Lần 3");
            facts.cap3 = c.good;
            s.set(c.good ? "ok" : "bad", this.describe(c));
            if (c.good) best = c;
          } else {
            facts.cap3 = false;
            s.set("bad", "không có bộ âm thanh nào chạy được");
          }
        }
      }
      facts.loud = best ? best.peak : 0;

      // 9. Vosk on what was captured
      s = this.step("Vosk nhận chữ từ đoạn vừa thu");
      if (!best) s.set("info", "bỏ qua: không thu được âm thanh");
      else {
        const v = await this.vosk(best.audio);
        facts.voskText = v.text || "";
        if (v.error) s.set("bad", v.error);
        else s.set(v.text ? "ok" : "bad", v.text ? `"${v.text}"` : "không ra chữ nào");
      }

      // 10. Whisper, if it is loaded
      if (best && R.whisper) {
        s = this.step("Whisper (chữ chính xác)");
        const w = await timed(R.whisper.call("run", best.audio), 30000);
        s.set(w.done && !w.err && w.val?.text ? "ok" : "warn",
          w.done ? (w.err ? errText(w.err) : `"${w.val?.text || ""}" (${(w.ms / 1000).toFixed(1)} s)`) : "quá 30 giây");
      }
    } catch (e) {
      if (e.message !== "stop") this.report.push(`[LỖI] kiểm tra dừng: ${errText(e)}`);
    } finally {
      for (const t of stream?.getTracks() || []) t.stop();
      for (const c of [tapCtx, ...extra]) { try { c.close(); } catch { /* */ } }
      this.meter.hidden = true;
      this.startBtn.disabled = false;
      this.startBtn.textContent = "↻ Kiểm tra lại";
    }
    this.showVerdict(facts);
  }

  showVerdict(f) {
    let kind = "ok", text;
    if (f.gum === "NotAllowedError" || f.gum === "SecurityError") {
      kind = "bad";
      text = "Micro chưa được cho phép. Vào Cài đặt → Safari → Micro (hoặc Cài đặt → Quyền riêng tư → Micro) → Cho phép, rồi mở lại app.";
    } else if (f.gum) {
      kind = "bad";
      text = `Không mở được micro (${f.gum}). Có thể app khác đang giữ micro (gọi điện, ghi âm, Zoom...). Đóng app đó rồi thử lại.`;
    } else if (f.trackMuted) {
      kind = "bad";
      text = "iPad đang tắt tiếng micro cho trang này (thường do cuộc gọi hoặc app khác dùng micro).";
    } else if (f.resumeHang && f.cap2) {
      kind = "bad";
      text = "NGUYÊN NHÂN: sau khi mở micro, bộ âm thanh tạo lúc chạm bị iPad treo — bản 9 chờ mãi ở bước này nên không nghe được. " +
             "Bộ âm thanh tạo sau khi mở micro thì chạy tốt (bản 10 đã tự chuyển sang cách này).";
    } else if (f.cap1 === false && (f.cap2 || f.cap3)) {
      kind = "bad";
      text = "NGUYÊN NHÂN: bộ âm thanh tạo lúc chạm chỉ nhận được im lặng từ micro (lỗi của Safari trên iPad). " +
             `Cách ${f.cap2 ? "tạo bộ âm thanh mới sau khi mở micro" : "ScriptProcessor"} thì nhận được tiếng (bản 10 tự chuyển sang cách này).`;
    } else if (f.cap1 === false) {
      kind = "bad";
      text = "Micro không đưa tiếng nào vào trang, kể cả bằng các cách khác. Kiểm tra micro bằng app Ghi âm; tháo tai nghe Bluetooth; khởi động lại iPad.";
    } else if (f.loud < 0.02) {
      kind = "warn";
      text = "Micro thu được nhưng tiếng rất nhỏ. Nói to hơn hoặc để iPad gần người nói hơn.";
    } else if (f.voskText === "") {
      kind = "bad";
      text = "Âm thanh vào đủ to nhưng Vosk không ra chữ — có thể tiếng bị méo. Hãy gửi kết quả này cho người phát triển.";
    } else if (!f.ready) {
      kind = "warn";
      text = "Micro và nhận chữ đều ổn, nhưng mô hình chưa sẵn sàng: app chỉ bắt đầu nghe khi tải/chuẩn bị xong. Nếu cứ đứng mãi ở \"Đang chuẩn bị\" thì đó là nguyên nhân.";
    } else {
      text = "Mọi bước đều ổn: micro thu được tiếng và nhận ra chữ tiếng Anh. Bấm ON để nghe.";
    }
    this.verdict.className = "diagVerdict " + kind;
    this.verdict.textContent = text;
    this.verdict.hidden = false;
    this.report.unshift(`Live Translator iPad — kiểm tra micro ${new Date().toLocaleString("vi-VN")}`, `KẾT LUẬN: ${text}`, "");
    this.copyBtn.hidden = false;
    this.verdict.scrollIntoView({ block: "nearest" });
  }

  async copy() {
    const text = this.report.join("\n") + `\n\n${navigator.userAgent}`;
    try {
      await navigator.clipboard.writeText(text);
      this.app.setStatus("✓ Đã copy kết quả kiểm tra micro — dán vào tin nhắn/email để gửi.", "ok");
    } catch {
      prompt("Copy kết quả này:", text);
    }
  }
}

function $status() {
  return document.getElementById("status")?.textContent || "";
}
