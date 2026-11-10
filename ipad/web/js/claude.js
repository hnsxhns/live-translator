// Claude API straight from the browser (Anthropic allows this with the direct-browser-access header).

export const MODEL = "claude-opus-5-5";
const API = "https://api.anthropic.com/v1";
const FALLBACK_BETA = "server-side-fallback-2026-07-01";   // refusal fallback: route to another model
export const FILES_BETA = "files-api-2025-04-14";            // documents uploaded to Claude (Learning library)

export class ClaudeError extends Error {
  constructor(status, type, message) {
    super(message || type || `HTTP ${status}`);
    this.status = status;
    this.type = type || "";
  }
}

function headers(apiKey, extra = {}) {
  return {
    "content-type": "application/json",
    "x-api-key": apiKey,
    "anthropic-version": "2023-06-01",
    "anthropic-dangerous-direct-browser-access": "true",
    ...extra,
  };
}

async function errorFrom(res) {
  let type = "", msg = "";
  try {
    const j = await res.json();
    type = j?.error?.type || "";
    msg = j?.error?.message || "";
  } catch { /* not JSON */ }
  return new ClaudeError(res.status, type, msg);
}

/** Checks that the key works (same as models.list(limit=1) on desktop). */
export async function checkKey(apiKey) {
  const res = await fetch(`${API}/models?limit=1`, { headers: headers(apiKey) });
  if (!res.ok) throw await errorFrom(res);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Stream a Claude reply; returns the full text. Retries once without the fallback option if rejected,
 * and once more on rate limits / overload / network errors (like the SDK's max_retries=1).
 * `prompt` is either the user's text or a full list of messages (multi-turn chat).
 */
export async function claudeStream(apiKey, { system, prompt, effort, onPiece, model, thinking, maxTokens = 4000,
                                             format, timeoutMs }) {
  const messages = Array.isArray(prompt) ? prompt : [{ role: "user", content: prompt }];
  const m = model || MODEL;
  const usesFiles = JSON.stringify(messages).includes('"type":"file"');
  for (const useFallback of [true, false]) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const body = { model: m, max_tokens: maxTokens, system, messages, stream: true,
                     output_config: { effort } };
      if (format) body.output_config.format = format;           // structured (JSON schema) output
      if (thinking) body.thinking = thinking;
      const betas = usesFiles ? [FILES_BETA] : [];
      if (useFallback && !m.startsWith("claude-haiku")) {     // Haiku has no fallback
        body.fallbacks = "default";
        betas.push(FALLBACK_BETA);
      }
      const extra = betas.length ? { "anthropic-beta": betas.join(",") } : {};
      // a stuck request must not block its caller for long (takeaways: 120 s)
      const ctrl = timeoutMs ? new AbortController() : null;
      const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
      let out = "";
      try {
        const res = await fetch(`${API}/messages`, { method: "POST", headers: headers(apiKey, extra),
                                                     body: JSON.stringify(body), signal: ctrl?.signal });
        if (!res.ok) throw await errorFrom(res);
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const event = buf.slice(0, i);
            buf = buf.slice(i + 2);
            for (const line of event.split("\n")) {
              if (!line.startsWith("data:")) continue;
              let data;
              try { data = JSON.parse(line.slice(5)); } catch { continue; }
              if (data.type === "content_block_delta" && data.delta?.type === "text_delta") {
                out += data.delta.text;
                onPiece?.(data.delta.text);
              } else if (data.type === "error") {
                throw new ClaudeError(0, data.error?.type, data.error?.message);
              }
            }
          }
        }
        return out;
      } catch (err) {
        const e = err?.name === "AbortError" ? new ClaudeError(0, "timeout", "Claude trả lời quá lâu (hết thời gian chờ)") : err;
        const msg = String(e?.message || e).toLowerCase();
        // only the optional fallback setting is worth retrying without
        if (useFallback && !out && e instanceof ClaudeError && e.status === 400 && msg.includes("fallback")) break;
        const retryable = !out && attempt === 0 && (!(e instanceof ClaudeError) || e.status === 429 ||
                          e.status >= 500 || e.type === "overloaded_error");
        if (retryable) { await sleep(1500); continue; }
        throw e;
      } finally {
        if (timer) clearTimeout(timer);
      }
    }
  }
  return "";
}

/** Upload a file to Claude (Files API) -> file id. */
export async function uploadFile(apiKey, blob, name) {
  const form = new FormData();
  form.append("file", blob, name);
  const h = headers(apiKey, { "anthropic-beta": FILES_BETA });
  delete h["content-type"];                                  // the browser sets the multipart boundary
  const res = await fetch(`${API}/files`, { method: "POST", headers: h, body: form });
  if (!res.ok) throw await errorFrom(res);
  return (await res.json()).id;
}

/** One Claude call that must answer with JSON matching `schema` (structured output when the request
 *  accepts it, otherwise plain JSON text). */
export async function askJson(apiKey, system, content, schema, { effort = "low", maxTokens = 16000 } = {}) {
  const prompt = typeof content === "string" ? content : [{ role: "user", content }];
  let last;
  for (const useFormat of [true, false]) {
    try {
      const text = await claudeStream(apiKey, { system, prompt, effort, maxTokens,
                                                format: useFormat ? { type: "json_schema", schema } : undefined });
      return parseJson(text);
    } catch (e) {
      last = e;
      if (e instanceof ClaudeError && e.status !== 400) throw e;
    }
  }
  throw last;
}

export function parseJson(text) {
  const t = text.trim().replace(/^```(?:json)?s*|s*```$/g, "");
  const a = t.indexOf("{"), b = t.lastIndexOf("}");
  if (a < 0 || b < a) throw new Error("Claude không trả về JSON");
  return JSON.parse(t.slice(a, b + 1));
}

/** Claude API errors -> short Vietnamese explanation. */
export function friendlyError(e) {
  const s = String(e?.message || e).toLowerCase();
  if (s.includes("credit balance")) {
    return ("Tài khoản API hết credit ($0). Gói Ultra có $200/tháng: vào claude.ai → Settings → Billing → " +
            "API credits → Link organization, rồi dùng API key của đúng tổ chức đó.");
  }
  if (e instanceof ClaudeError) {
    if (e.status === 401) return "API key không còn hợp lệ. Hãy đăng nhập lại.";
    if (e.status === 403) return "Tài khoản không có quyền dùng model này.";
    if (e.status === 429) return "Đang gửi quá nhiều yêu cầu (rate limit). Ứng dụng sẽ tự thử lại.";
    return `Lỗi Claude: ${String(e.message).slice(0, 160)}`;
  }
  if (e instanceof TypeError) return "Mất kết nối tới Claude. Kiểm tra mạng.";
  return `Lỗi Claude: ${String(e?.message || e).slice(0, 160)}`;
}
