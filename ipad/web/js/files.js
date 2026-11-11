// Files attached to a question for Claude: images, PDF, Word .docx and plain-text files.

const IMAGE_TYPES = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp" };
const TEXT_TYPES = new Set(["txt", "md", "csv", "tsv", "json", "xml", "html", "htm", "log", "srt", "vtt", "yaml", "yml",
  "py", "js", "ts", "java", "cs", "cpp", "c", "h", "go", "rb", "php", "sql", "ini"]);
export const ACCEPT = ["image/*", ".pdf", ".docx", ...[...TEXT_TYPES].map((e) => "." + e)].join(",");

const MB = 2 ** 20;

function ext(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

function b64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

/** iPad photos are often HEIC or very large: re-encode to a JPEG Claude accepts (max 2048 px, < 5 MB). */
async function imageBlock(file) {
  const e = ext(file.name);
  if (IMAGE_TYPES[e] && file.size <= 5 * MB && file.type !== "image/heic") {
    return { type: "image", source: { type: "base64", media_type: IMAGE_TYPES[e], data: b64(await file.arrayBuffer()) } };
  }
  let bmp;
  try {
    bmp = await createImageBitmap(file);
  } catch {
    throw new Error("không đọc được ảnh này");
  }
  const scale = Math.min(1, 2048 / Math.max(bmp.width, bmp.height));
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * scale);
  c.height = Math.round(bmp.height * scale);
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  for (const q of [0.85, 0.7, 0.5]) {
    const blob = await new Promise((r) => c.toBlob(r, "image/jpeg", q));
    if (blob && blob.size <= 5 * MB) {
      return { type: "image", source: { type: "base64", media_type: "image/jpeg", data: b64(await blob.arrayBuffer()) } };
    }
  }
  throw new Error("ảnh lớn hơn 5 MB");
}

/** Read a file into a Claude content block. Throws Error with a Vietnamese reason if unsupported. */
export async function fileToBlock(file) {
  const e = ext(file.name);
  if (IMAGE_TYPES[e] || file.type.startsWith("image/")) return imageBlock(file);
  if (e === "pdf") {
    if (file.size > 20 * MB) throw new Error("PDF lớn hơn 20 MB");
    return { type: "document", title: file.name,
             source: { type: "base64", media_type: "application/pdf", data: b64(await file.arrayBuffer()) } };
  }
  let text;
  if (e === "docx") {
    text = await docxText(file);
  } else if (TEXT_TYPES.has(e) || file.type.startsWith("text/")) {
    const raw = new Uint8Array(await file.arrayBuffer());
    for (const enc of ["utf-8", "utf-16le", "windows-1258", "latin1"]) {
      try {
        text = new TextDecoder(enc, { fatal: true }).decode(raw);
        break;
      } catch { /* try the next encoding */ }
    }
    if (text === undefined) text = new TextDecoder("latin1").decode(raw);
    text = text.replace(/^﻿/, "");
  } else {
    throw new Error("loại file chưa hỗ trợ (dùng ảnh, PDF, Word .docx hoặc file chữ)");
  }
  if (text.length > 400_000) throw new Error("file chữ quá dài (hơn 400.000 ký tự)");
  return { type: "text", text: `<file name="${file.name}">\n${text}\n</file>` };
}

// ---- minimal .zip reader (enough for .docx) ----
export async function unzipEntry(buf, wanted) {
  const v = new DataView(buf);
  let eocd = -1;
  for (let i = buf.byteLength - 22; i >= Math.max(0, buf.byteLength - 66000); i--) {
    if (v.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error("không phải file zip");
  const count = v.getUint16(eocd + 10, true);
  let p = v.getUint32(eocd + 16, true);
  const dec = new TextDecoder();
  for (let k = 0; k < count; k++) {
    if (v.getUint32(p, true) !== 0x02014b50) break;
    const method = v.getUint16(p + 10, true);
    const csize = v.getUint32(p + 20, true);
    const nlen = v.getUint16(p + 28, true), xlen = v.getUint16(p + 30, true), clen = v.getUint16(p + 32, true);
    const local = v.getUint32(p + 42, true);
    const name = dec.decode(new Uint8Array(buf, p + 46, nlen));
    if (name === wanted) {
      const start = local + 30 + v.getUint16(local + 26, true) + v.getUint16(local + 28, true);
      const data = new Uint8Array(buf, start, csize);
      if (method === 0) return data;
      if (method !== 8) throw new Error("kiểu nén không hỗ trợ");
      const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
      return new Uint8Array(await new Response(stream).arrayBuffer());
    }
    p += 46 + nlen + xlen + clen;
  }
  throw new Error(`thiếu ${wanted}`);
}

/** Plain text of a Word .docx (paragraphs and table cells). */
export async function docxText(file) {
  let xml;
  try {
    xml = new TextDecoder().decode(await unzipEntry(await file.arrayBuffer(), "word/document.xml"));
  } catch (e) {
    throw new Error(`không đọc được file Word (${e.message})`);
  }
  const ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  const paras = [];
  for (const p of doc.getElementsByTagNameNS(ns, "p")) {
    paras.push([...p.getElementsByTagNameNS(ns, "t")].map((t) => t.textContent).join(""));
  }
  return paras.join("\n").trim();
}
