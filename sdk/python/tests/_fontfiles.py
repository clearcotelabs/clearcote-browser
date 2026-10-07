"""Tiny synthetic font files for the font tests: a name table, a cmap (format 4 for the BMP, format 12
beyond it) and optionally a colour table -- just what clearcote._fonts reads. No real fonts needed."""
import struct


def _name_table(families):
    """Name ID 1 (and 16 for a second name) in UTF-16BE, platform 3."""
    records, strings = [], b""
    for name_id, text in zip((1, 16), families):
        raw = text.encode("utf-16-be")
        records.append(struct.pack(">6H", 3, 1, 0x409, name_id, len(raw), len(strings)))
        strings += raw
    return struct.pack(">HHH", 0, len(records), 6 + 12 * len(records)) + b"".join(records) + strings


def _cmap4(cps, via_glyph_array=()):
    """One segment per code point (glyph 1+i); those in ``via_glyph_array`` go through idRangeOffset
    and glyphIdArray instead of idDelta. Ends with the required 0xFFFF segment."""
    segs = [(c, i + 1) for i, c in enumerate(sorted(cps))] + [(0xFFFF, 0)]
    n = len(segs)
    ends = b"".join(struct.pack(">H", c) for c, _g in segs)
    starts = ends
    deltas, offsets, glyphs = b"", b"", b""
    for i, (c, g) in enumerate(segs):
        if c in via_glyph_array:
            deltas += struct.pack(">H", 0)
            offsets += struct.pack(">H", 2 * (n - i) + len(glyphs))  # from this entry to its glyphIdArray slot
            glyphs += struct.pack(">H", g)
        else:
            deltas += struct.pack(">H", ((g - c) & 0xFFFF) if g else 1)
            offsets += struct.pack(">H", 0)
    rest = struct.pack(">HHHH", n * 2, 0, 0, 0) + ends + b"\0\0" + starts + deltas + offsets + glyphs
    return struct.pack(">HHH", 4, 6 + len(rest), 0) + rest


def _cmap12(cps):
    groups = b"".join(struct.pack(">III", c, c, i + 1) for i, c in enumerate(sorted(cps)))
    return struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, len(cps)) + groups


def _cmap(cps, via_glyph_array=()):
    subs = [(3, 1, _cmap4([c for c in cps if c <= 0xFFFF], via_glyph_array))]
    if any(c > 0xFFFF for c in cps):
        subs.append((3, 10, _cmap12(cps)))
    recs, data, off = b"", b"", 4 + 8 * len(subs)
    for platform, encoding, sub in subs:
        recs += struct.pack(">HHI", platform, encoding, off + len(data))
        data += sub
    return struct.pack(">HH", 0, len(subs)) + recs + data


def _tables(families, cps=(), color=False, via_glyph_array=()):
    tables = {b"name": _name_table(families)}
    if cps:
        tables[b"cmap"] = _cmap(cps, via_glyph_array)
    if color:
        tables[b"COLR"] = b"\0" * 14
    return tables


def _sfnt(tables, base=0):
    """One face whose table offsets are absolute, starting at file offset ``base``."""
    tags = sorted(tables)
    head = struct.pack(">IHHHH", 0x00010000, len(tags), 0, 0, 0)
    off, recs, data = base + 12 + 16 * len(tags), b"", b""
    for tag in tags:
        recs += struct.pack(">4sIII", tag, 0, off + len(data), len(tables[tag]))
        data += tables[tag] + b"\0" * (-len(tables[tag]) % 4)
    return head + recs + data


def make_font(path, families, cps=(), color=False, via_glyph_array=()):
    path.write_bytes(_sfnt(_tables(families, cps, color, via_glyph_array)))
    return str(path)


def make_collection(path, faces):
    """A .ttc of several faces, each ``(families, cps)``."""
    pos = 12 + 4 * len(faces)
    blobs, offsets = [], []
    for families, cps in faces:
        blob = _sfnt(_tables(families, cps), base=pos)
        offsets.append(pos)
        blobs.append(blob)
        pos += len(blob)
    head = b"ttcf" + struct.pack(">II", 0x00010000, len(faces)) + b"".join(struct.pack(">I", o) for o in offsets)
    path.write_bytes(head + b"".join(blobs))
    return str(path)
