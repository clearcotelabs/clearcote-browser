// Tiny synthetic font files for the font tests: a name table, a cmap (format 4 for the BMP, format 12
// beyond it) and optionally a colour table -- just what src/fonts.ts reads. No real fonts needed.
// Mirrors sdk/python/tests/_fontfiles.py.
import { writeFileSync } from "node:fs";

const u16 = (...v: number[]) => { const b = Buffer.alloc(2 * v.length); v.forEach((x, i) => b.writeUInt16BE(x & 0xffff, 2 * i)); return b; };
const u32 = (...v: number[]) => { const b = Buffer.alloc(4 * v.length); v.forEach((x, i) => b.writeUInt32BE(x >>> 0, 4 * i)); return b; };

function nameTable(families: string[]): Buffer {
  const records: Buffer[] = [];
  let strings = Buffer.alloc(0);
  families.slice(0, 2).forEach((text, i) => {
    const raw = Buffer.from(text, "utf16le").swap16();
    records.push(u16(3, 1, 0x409, i === 0 ? 1 : 16, raw.length, strings.length));
    strings = Buffer.concat([strings, raw]);
  });
  return Buffer.concat([u16(0, records.length, 6 + 12 * records.length), ...records, strings]);
}

function cmap4(cps: number[], viaGlyphArray: number[] = []): Buffer {
  const segs: Array<[number, number]> = [...cps].sort((a, b) => a - b).map((c, i) => [c, i + 1]);
  segs.push([0xffff, 0]);
  const n = segs.length;
  const ends = u16(...segs.map(([c]) => c));
  const deltas: number[] = [], offsets: number[] = [], glyphs: number[] = [];
  segs.forEach(([c, g], i) => {
    if (viaGlyphArray.includes(c)) {
      deltas.push(0);
      offsets.push(2 * (n - i) + 2 * glyphs.length); // from this entry to its glyphIdArray slot
      glyphs.push(g);
    } else {
      deltas.push(g ? (g - c) & 0xffff : 1);
      offsets.push(0);
    }
  });
  const rest = Buffer.concat([u16(n * 2, 0, 0, 0), ends, u16(0), ends, u16(...deltas), u16(...offsets), u16(...glyphs)]);
  return Buffer.concat([u16(4, 6 + rest.length, 0), rest]);
}

function cmap12(cps: number[]): Buffer {
  const groups = Buffer.concat([...cps].sort((a, b) => a - b).map((c, i) => u32(c, c, i + 1)));
  return Buffer.concat([u16(12, 0), u32(16 + groups.length, 0, cps.length), groups]);
}

function cmap(cps: number[], viaGlyphArray: number[] = []): Buffer {
  const subs: Array<[number, number, Buffer]> = [[3, 1, cmap4(cps.filter((c) => c <= 0xffff), viaGlyphArray)]];
  if (cps.some((c) => c > 0xffff)) subs.push([3, 10, cmap12(cps)]);
  const recs: Buffer[] = [];
  let data = Buffer.alloc(0);
  const off = 4 + 8 * subs.length;
  for (const [platform, encoding, sub] of subs) {
    recs.push(Buffer.concat([u16(platform, encoding), u32(off + data.length)]));
    data = Buffer.concat([data, sub]);
  }
  return Buffer.concat([u16(0, subs.length), ...recs, data]);
}

function tablesFor(families: string[], cps: number[] = [], color = false, viaGlyphArray: number[] = []): Map<string, Buffer> {
  const t = new Map<string, Buffer>([["name", nameTable(families)]]);
  if (cps.length) t.set("cmap", cmap(cps, viaGlyphArray));
  if (color) t.set("COLR", Buffer.alloc(14));
  return t;
}

/** One face whose table offsets are absolute, starting at file offset `base`. */
function sfnt(tables: Map<string, Buffer>, base = 0): Buffer {
  const tags = [...tables.keys()].sort();
  const head = Buffer.concat([u32(0x00010000), u16(tags.length, 0, 0, 0)]);
  let off = base + 12 + 16 * tags.length;
  const recs: Buffer[] = [], data: Buffer[] = [];
  for (const tag of tags) {
    const blob = tables.get(tag)!;
    recs.push(Buffer.concat([Buffer.from(tag, "latin1"), u32(0, off, blob.length)]));
    const padded = Buffer.concat([blob, Buffer.alloc((4 - (blob.length % 4)) % 4)]);
    data.push(padded);
    off += padded.length;
  }
  return Buffer.concat([head, ...recs, ...data]);
}

export function makeFont(path: string, families: string[], cps: number[] = [],
  opts: { color?: boolean; viaGlyphArray?: number[] } = {}): string {
  writeFileSync(path, sfnt(tablesFor(families, cps, opts.color, opts.viaGlyphArray)));
  return path;
}

/** A .ttc of several faces, each `[families, cps]`. */
export function makeCollection(path: string, faces: Array<[string[], number[]]>): string {
  let pos = 12 + 4 * faces.length;
  const blobs: Buffer[] = [], offsets: number[] = [];
  for (const [families, cps] of faces) {
    const blob = sfnt(tablesFor(families, cps), pos);
    offsets.push(pos);
    blobs.push(blob);
    pos += blob.length;
  }
  writeFileSync(path, Buffer.concat([Buffer.from("ttcf", "latin1"), u32(0x00010000, faces.length), u32(...offsets), ...blobs]));
  return path;
}
