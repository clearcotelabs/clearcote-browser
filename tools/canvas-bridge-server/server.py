#!/usr/bin/env python3
"""Canvas-bridge REAL render server.

Unlike the stub, this renders forwarded ops on an actual **headless clearcote**
(real Chromium canvas pipeline on the host GPU), so the pixels it returns are
genuine Chrome output for the server host's GPU. It:

  * speaks the clearcote LE codec over RFC 6455 (async),
  * launches one headless clearcote and reuses its page,
  * buffers Canvas2D ops per server-side canvas id and, on getImageData,
    replays them on a real OffscreenCanvas via page.evaluate and returns the
    actual getImageData bytes.

This is the "headless clearcote server" the docs spec as --canvas-bridge-server.

    python cb_render_server.py --chrome <path> [--port 9099] [--fingerprint 99117]
"""
import argparse
import asyncio
import base64
import hashlib
import json
import struct
import sys

from playwright.async_api import async_playwright

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HELLO, WELCOME, CREATE_CANVAS2D, GET_IMAGE_DATA, IMAGE_DATA, CANVAS2D_OP, MEASURE_TEXT = 1, 2, 3, 4, 5, 6, 7


# ---- async RFC 6455 framing (client frames are masked; ours are not) ----
async def read_exact(reader, n):
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError:
        return None


async def ws_handshake(reader, writer):
    req = b""
    while b"\r\n\r\n" not in req:
        chunk = await reader.read(1024)
        if not chunk:
            return False
        req += chunk
    key = ""
    for line in req.decode("latin1").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
    await writer.drain()
    return True


async def read_ws_frame(reader):
    while True:
        h = await read_exact(reader, 2)
        if not h:
            return None
        opcode, masked, length = h[0] & 0x0F, (h[1] & 0x80) != 0, h[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", await read_exact(reader, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await read_exact(reader, 8))[0]
        mask = await read_exact(reader, 4) if masked else b"\0\0\0\0"
        payload = await read_exact(reader, length) if length else b""
        if payload is None:
            return None
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            return None
        if opcode in (0x9, 0xA):
            continue
        return payload


async def write_ws_frame(writer, payload):
    header = bytearray([0x82])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126); header += struct.pack("!H", n)
    else:
        header.append(127); header += struct.pack("!Q", n)
    writer.write(bytes(header) + payload)
    await writer.drain()


class Reader:
    def __init__(self, data): self.d, self.o = data, 0
    def u32(self): v = struct.unpack_from("<I", self.d, self.o)[0]; self.o += 4; return v
    def i32(self): v = struct.unpack_from("<i", self.d, self.o)[0]; self.o += 4; return v
    def u64(self): v = struct.unpack_from("<Q", self.d, self.o)[0]; self.o += 8; return v
    def f64(self): v = struct.unpack_from("<d", self.d, self.o)[0]; self.o += 8; return v
    def s(self):
        n = self.u32(); v = self.d[self.o:self.o + n].decode("utf-8", "replace"); self.o += n; return v


def enc_welcome():
    out = struct.pack("<I", WELCOME) + struct.pack("<I", 1)
    for field in ["clearcote-render-server/1", "Windows", "Google Inc. (Intel)",
                  "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11, D3D11)"]:
        b = field.encode("utf-8"); out += struct.pack("<I", len(b)) + b
    return out


def enc_image_data(cid, w, h, pixels):
    return (struct.pack("<I", IMAGE_DATA) + struct.pack("<I", cid) + struct.pack("<I", w) +
            struct.pack("<I", h) + struct.pack("<I", len(pixels)) + pixels)


# ---- Canvas2D op id -> JS statement (matches Canvas2DOp in the codec) ----
def op_to_js(op, s, a):
    S = json.dumps(s)
    def n(i, d=0.0): return a[i] if i < len(a) else d
    table = {
        1: "ctx.save();", 2: "ctx.restore();", 3: "ctx.beginPath();",
        4: "ctx.closePath();", 5: "ctx.fill();", 6: "ctx.stroke();", 7: "ctx.clip();",
        10: f"ctx.fillRect({n(0)},{n(1)},{n(2)},{n(3)});",
        11: f"ctx.strokeRect({n(0)},{n(1)},{n(2)},{n(3)});",
        12: f"ctx.clearRect({n(0)},{n(1)},{n(2)},{n(3)});",
        20: f"ctx.fillText({S},{n(0)},{n(1)}{(','+repr(n(2))) if len(a)>2 else ''});",
        21: f"ctx.strokeText({S},{n(0)},{n(1)});",
        30: f"ctx.moveTo({n(0)},{n(1)});", 31: f"ctx.lineTo({n(0)},{n(1)});",
        32: f"ctx.rect({n(0)},{n(1)},{n(2)},{n(3)});",
        33: f"ctx.arc({n(0)},{n(1)},{n(2)},{n(3)},{n(4)}{(',true' if len(a)>5 and a[5] else '') });",
        35: f"ctx.quadraticCurveTo({n(0)},{n(1)},{n(2)},{n(3)});",
        36: f"ctx.bezierCurveTo({n(0)},{n(1)},{n(2)},{n(3)},{n(4)},{n(5)});",
        34: f"ctx.arcTo({n(0)},{n(1)},{n(2)},{n(3)},{n(4)});",
        37: f"ctx.ellipse({n(0)},{n(1)},{n(2)},{n(3)},{n(4)},{n(5)},{n(6)});",
        40: f"ctx.setTransform({n(0)},{n(1)},{n(2)},{n(3)},{n(4)},{n(5)});",
        41: f"ctx.transform({n(0)},{n(1)},{n(2)},{n(3)},{n(4)},{n(5)});",
        42: f"ctx.translate({n(0)},{n(1)});", 43: f"ctx.rotate({n(0)});",
        44: f"ctx.scale({n(0)},{n(1)});", 45: "ctx.resetTransform();",
        50: f"ctx.fillStyle={S};", 51: f"ctx.strokeStyle={S};", 52: f"ctx.lineWidth={n(0)};",
        53: f"ctx.font={S};", 54: f"ctx.textAlign={S};", 55: f"ctx.textBaseline={S};",
        56: f"ctx.globalAlpha={n(0)};", 57: f"ctx.globalCompositeOperation={S};",
        58: f"ctx.lineCap={S};", 59: f"ctx.lineJoin={S};",
    }
    return table.get(op, "")


def build_render_js(cw, ch, ops, x, y, w, h):
    body = (f"const c=new OffscreenCanvas({cw},{ch});const ctx=c.getContext('2d');"
            + "".join(ops)
            + f"const d=ctx.getImageData({x},{y},{w},{h});const u=new Uint8Array(d.data.buffer);"
            + "let s='';const CH=16384;for(let i=0;i<u.length;i+=CH)"
            + "s+=String.fromCharCode.apply(null,u.subarray(i,i+CH));return btoa(s);")
    return "() => {" + body + "}"


def build_measure_js(cw, ch, ops, text):
    body = (f"const c=new OffscreenCanvas({cw},{ch});const ctx=c.getContext('2d');"
            + "".join(ops)
            + f"const m=ctx.measureText({json.dumps(text)});"
            + "return [m.width,m.actualBoundingBoxLeft,m.actualBoundingBoxRight,"
            + "m.fontBoundingBoxAscent,m.fontBoundingBoxDescent,"
            + "m.actualBoundingBoxAscent,m.actualBoundingBoxDescent,"
            + "m.emHeightAscent,m.emHeightDescent];")
    return "() => {" + body + "}"


async def handle_client(reader, writer, page, lock):
    if not await ws_handshake(reader, writer):
        return
    print("[server] client connected", flush=True)
    canvases = {}  # id -> {w,h,ops:[]}
    try:
        while True:
            payload = await read_ws_frame(reader)
            if payload is None:
                break
            r = Reader(payload)
            t = r.u32()
            if t == HELLO:
                r.u32(); print("[server] Hello seed=%d ver=%r" % (r.u64(), r.s()), flush=True)
                await write_ws_frame(writer, enc_welcome())
            elif t == CREATE_CANVAS2D:
                cid, cw, ch = r.u32(), r.u32(), r.u32()
                canvases[cid] = {"w": max(cw, 1), "h": max(ch, 1), "ops": []}
                print("[server] CreateCanvas2D id=%d %dx%d" % (cid, cw, ch), flush=True)
            elif t == CANVAS2D_OP:
                cid, op = r.u32(), r.u32(); s = r.s(); args = [r.f64() for _ in range(r.u32())]
                cv = canvases.get(cid)
                if cv is not None:
                    cv["ops"].append(op_to_js(op, s, args))
            elif t == GET_IMAGE_DATA:
                cid, x, y, w, h = r.u32(), r.i32(), r.i32(), r.u32(), r.u32()
                cv = canvases.get(cid, {"w": w, "h": h, "ops": []})
                js = build_render_js(cv["w"], cv["h"], cv["ops"], x, y, w, h)
                async with lock:
                    try:
                        b64 = await page.evaluate(js)
                        pixels = base64.b64decode(b64)
                    except Exception as e:  # noqa: BLE001
                        print("[server] render error:", e, flush=True)
                        pixels = bytes(w * h * 4)
                if len(pixels) != w * h * 4:
                    pixels = (pixels + bytes(w * h * 4))[: w * h * 4]
                print("[server] rendered id=%d %dx%d (%d ops) -> %d bytes" %
                      (cid, w, h, len(cv["ops"]), len(pixels)), flush=True)
                await write_ws_frame(writer, enc_image_data(cid, w, h, pixels))
            elif t == MEASURE_TEXT:
                cid = r.u32()
                text = r.s()
                cv = canvases.get(cid, {"w": 1, "h": 1, "ops": []})
                js = build_measure_js(cv["w"], cv["h"], cv["ops"], text)
                async with lock:
                    try:
                        vals = await page.evaluate(js)
                    except Exception as e:  # noqa: BLE001
                        print("[server] measureText error:", e, flush=True)
                        vals = [0.0] * 9
                metrics = b"".join(struct.pack("<d", float(v)) for v in vals)
                print("[server] measureText id=%d %r -> width=%.2f" %
                      (cid, text, vals[0] if vals else 0.0), flush=True)
                await write_ws_frame(writer, enc_image_data(cid, 9, 1, metrics))
    finally:
        writer.close()
        print("[server] client gone", flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", required=True)
    ap.add_argument("--port", type=int, default=9099)
    ap.add_argument("--fingerprint", default="99117")
    args = ap.parse_args()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=args.chrome, headless=True,
            args=["--no-first-run", "--no-sandbox", "--fingerprint=" + args.fingerprint],
            ignore_default_args=["--enable-automation"])
        page = await browser.new_page()
        await page.goto("about:blank")
        lock = asyncio.Lock()
        print("[server] headless clearcote up; listening on ws://127.0.0.1:%d" % args.port, flush=True)
        srv = await asyncio.start_server(
            lambda r, w: handle_client(r, w, page, lock), "127.0.0.1", args.port)
        async with srv:
            await srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
