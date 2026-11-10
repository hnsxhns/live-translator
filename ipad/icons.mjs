// App icons, drawn in code so the repo needs no image files.
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";

export function makeIcons(dir, sizes = [180, 192, 512]) {
  fs.mkdirSync(dir, { recursive: true });
  for (const size of sizes) fs.writeFileSync(path.join(dir, `icon-${size}.png`), png(size, iconPixel));
}

export function iconPixel(x, y) {             // x, y in 0..1 -> [r, g, b]
  const rr = (x0, y0, x1, y1, r) => {
    const cx = Math.min(Math.max(x, x0 + r), x1 - r), cy = Math.min(Math.max(y, y0 + r), y1 - r);
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r && x >= x0 && x <= x1 && y >= y0 && y <= y1;
  };
  const tri = (ax, ay, bx, by, cx, cy) => {
    const s = (px, py, qx, qy, rx, ry) => (px - rx) * (qy - ry) - (qx - rx) * (py - ry);
    const d1 = s(x, y, ax, ay, bx, by), d2 = s(x, y, bx, by, cx, cy), d3 = s(x, y, cx, cy, ax, ay);
    return !((d1 < 0 || d2 < 0 || d3 < 0) && (d1 > 0 || d2 > 0 || d3 > 0));
  };
  const bar = (x0, x1, y0) => x >= x0 && x <= x1 && y >= y0 && y <= y0 + 0.045;
  const ORANGE = [217, 119, 87], WHITE = [255, 255, 255], DARK = [43, 43, 43];
  let c = ORANGE;
  if (rr(0.13, 0.17, 0.67, 0.57, 0.09) || tri(0.22, 0.55, 0.36, 0.55, 0.19, 0.69)) c = WHITE;
  if (bar(0.22, 0.58, 0.29) || bar(0.22, 0.47, 0.40)) c = ORANGE;
  if (rr(0.33, 0.43, 0.87, 0.81, 0.09) || tri(0.66, 0.79, 0.80, 0.79, 0.83, 0.92)) c = DARK;
  if (bar(0.42, 0.78, 0.56) || bar(0.42, 0.66, 0.67)) c = c === DARK ? WHITE : c;
  return c;
}

export function png(size, pixel) {
  const SS = 4;
  const raw = Buffer.alloc((size * 3 + 1) * size);
  for (let y = 0; y < size; y++) {
    raw[y * (size * 3 + 1)] = 0;
    for (let x = 0; x < size; x++) {
      const acc = [0, 0, 0];
      for (let sy = 0; sy < SS; sy++) for (let sx = 0; sx < SS; sx++) {
        const c = pixel((x + (sx + 0.5) / SS) / size, (y + (sy + 0.5) / SS) / size);
        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2];
      }
      const o = y * (size * 3 + 1) + 1 + x * 3;
      for (let k = 0; k < 3; k++) raw[o + k] = Math.round(acc[k] / (SS * SS));
    }
  }
  const crcTable = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc = (buf) => {
    let c = 0xffffffff;
    for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const td = Buffer.concat([Buffer.from(type), data]);
    const c = Buffer.alloc(4); c.writeUInt32BE(crc(td));
    return Buffer.concat([len, td, c]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0); ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr),
                        chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}
