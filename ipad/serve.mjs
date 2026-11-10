// Local test server for ipad/site/:  node serve.mjs [port]   ->  http://localhost:8787/
// (Microphone access needs HTTPS or localhost, so an iPad on the network needs the hosted version.)
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SITE = path.join(path.dirname(fileURLToPath(import.meta.url)), "site");
const PORT = +(process.argv[2] || 8787);
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript",
  ".css": "text/css", ".json": "application/json", ".webmanifest": "application/manifest+json", ".png": "image/png",
  ".wasm": "application/wasm", ".onnx": "application/octet-stream", ".gz": "application/octet-stream",
  ".txt": "text/plain" };

http.createServer((req, res) => {
  let p = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (p.endsWith("/")) p += "index.html";
  const file = path.join(SITE, path.normalize(p));
  if (!file.startsWith(SITE) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end("not found");
    return;
  }
  const size = fs.statSync(file).size;
  res.writeHead(200, { "content-type": TYPES[path.extname(file)] || "application/octet-stream",
                       "content-length": size, "cache-control": "no-cache" });
  if (req.method === "HEAD") return res.end();
  fs.createReadStream(file).pipe(res);
}).listen(PORT, () => console.log(`Live Translator iPad: http://localhost:${PORT}/`));
