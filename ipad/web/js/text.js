// One sentence per line (same rules as the desktop app).

const SENTENCE_GAP = /[.!?…](\s+)(?=\S)/g;     // the space after a sentence end
const ABBREV = new Set(["mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "no", "fig",
                        "ông", "bà", "tp"]);

const isUpper = (c) => c !== c.toLowerCase() && c === c.toUpperCase();

/** [start, end] of each space that separates two sentences. */
export function sentenceGaps(text) {
  const out = [];
  for (const m of text.matchAll(SENTENCE_GAP)) {
    const start = m.index + 1, end = m.index + m[0].length;
    const nxt = text[end] || "";
    const word = text.slice(0, start - 1).match(/[\p{L}\p{N}_.]+$/u);
    if ((isUpper(nxt) || /\d/.test(nxt) || "\"'“(".includes(nxt)) && !(word && ABBREV.has(word[0].toLowerCase()))) {
      out.push([start, end]);
    }
  }
  return out;
}

export function splitSentences(text) {
  let out = "", last = 0;
  for (const [a, b] of sentenceGaps(text)) {
    out += text.slice(last, a) + "\n";
    last = b;
  }
  return out + text.slice(last);
}
