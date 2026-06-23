#!/usr/bin/env python3
"""Canvas-bridge render server.

Replays the canvas/WebGL operations forwarded by a clearcote client on a *real*
browser and returns the genuine readback pixels. Whatever GPU that browser runs on
becomes the canvas/WebGL identity your clients present, so the readback APIs
(getImageData / toDataURL / readPixels / measureText) stay coherent with real
hardware instead of the scraper host's GPU.

Two render backends:

  --backend local --chrome <path>
        Launch a local headless clearcote and render on THIS host's GPU. Simple, but
        the canvas identity is your own machine's GPU.

  --backend cdp --cdp-url <ws(s)://...>
        Connect over the Chrome DevTools Protocol to a browser running somewhere with
        the GPU you want to present, and render there. **Bring your own browser host.**
        Anything that exposes a CDP/WebSocket endpoint works: a cloud browser service,
        or a browser you run on a spare machine with `--remote-debugging-port`. Pick a
        host whose GPU is a *real consumer GPU* (not a datacenter card or a software
        rasterizer) if you want a plausible consumer canvas identity.
        See get_cdp_url() to plug a provider that mints short-lived sessions via an API.

The server speaks the clearcote little-endian bridge codec over RFC 6455 (async),
buffers ops per server-side canvas id, and on a readback request replays them on a
real OffscreenCanvas via page.evaluate.

    python server.py --backend local --chrome <path> [--port 9099]
    python server.py --backend cdp --cdp-url ws://127.0.0.1:9222/... [--port 9099]
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
CREATE_WEBGL, WEBGL_OP = 8, 9


# ============================================================================
#  Render backends. Each exposes:
#     await backend.start()        -> establish a Playwright Page (backend.page)
#     await backend.ensure()       -> (re)connect if the page/session died
#     backend.gpu_vendor/renderer  -> real GPU strings for the Welcome message
#     await backend.close()
#  handle_client() only touches backend.page + backend.lock, so the rest of the
#  server is identical for every backend.
# ============================================================================


async def _probe_gpu(page):
    """Read the render browser's REAL WebGL vendor/renderer (reported in Welcome)."""
    try:
        info = await page.evaluate(
            "() => { const c=new OffscreenCanvas(1,1);"
            "const gl=c.getContext('webgl')||c.getContext('experimental-webgl');"
            "if(!gl)return null;"
            "const e=gl.getExtension('WEBGL_debug_renderer_info');"
            "return {vendor: e?gl.getParameter(e.UNMASKED_VENDOR_WEBGL):gl.getParameter(gl.VENDOR),"
            "renderer: e?gl.getParameter(e.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER)};}")
        if info:
            return info.get("vendor") or "", info.get("renderer") or ""
    except Exception as e:  # noqa: BLE001
        print("[backend] GPU probe failed:", e, flush=True)
    return "", ""


async def _warm(page):
    """Touch the GL + 2D pipelines so the first client-facing readback skips cold start."""
    try:
        await page.evaluate(
            "() => { const g=new OffscreenCanvas(8,8).getContext('webgl');"
            "if(g){g.clear(g.COLOR_BUFFER_BIT);}"
            "const x=new OffscreenCanvas(8,8).getContext('2d');"
            "x.fillRect(0,0,8,8); x.getImageData(0,0,8,8); }")
    except Exception:  # noqa: BLE001
        pass


class LocalBackend:
    """Render on a local headless clearcote (this host's GPU)."""

    def __init__(self, pw, chrome, fingerprint):
        self.pw, self.chrome, self.fingerprint = pw, chrome, fingerprint
        self.browser = self.page = None
        self.lock = asyncio.Lock()
        self.gpu_vendor = self.gpu_renderer = ""

    async def start(self):
        self.browser = await self.pw.chromium.launch(
            executable_path=self.chrome, headless=True,
            args=["--no-first-run", "--no-sandbox",
                  "--fingerprint=" + self.fingerprint] if self.fingerprint else
                 ["--no-first-run", "--no-sandbox"],
            ignore_default_args=["--enable-automation"])
        self.page = await self.browser.new_page()
        await self.page.goto("about:blank")
        self.gpu_vendor, self.gpu_renderer = await _probe_gpu(self.page)
        await _warm(self.page)
        print("[backend] local headless browser up; GPU=%r" % self.gpu_renderer, flush=True)

    async def ensure(self):
        if self.page is None or self.page.is_closed():
            await self.start()

    async def close(self):
        if self.browser:
            await self.browser.close()


class RemoteCDPBackend:
    """Render on ANY browser reachable over the Chrome DevTools Protocol.

    Bring your own browser host. `cdp_url` is a CDP/WebSocket endpoint to a browser
    running on the GPU you want to present.

    Providers that issue short-lived sessions via an API: override get_cdp_url() to
    (1) call your provider to create a session and (2) return its CDP WebSocket URL,
    and stop the session in close() (closing the CDP connection usually does NOT end
    the remote session or its billing -- you must call the provider's stop endpoint).
    Keep credentials in the environment; never hard-code them.
    """

    def __init__(self, pw, cdp_url):
        self.pw, self.cdp_url = pw, cdp_url
        self.browser = self.page = None
        self.lock = asyncio.Lock()
        self.gpu_vendor = self.gpu_renderer = ""

    def get_cdp_url(self):
        if not self.cdp_url:
            raise SystemExit("--backend cdp requires --cdp-url (or override get_cdp_url())")
        return self.cdp_url

    async def _connect(self, url):
        self.browser = await self.pw.chromium.connect_over_cdp(url, timeout=90000)
        # A CDP session usually has a pre-existing context + page; fall back if not.
        ctx = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        self.page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await self.page.goto("about:blank")
        except Exception:  # noqa: BLE001 -- some hosts reject navigation; evaluate still works
            pass
        v, r = await _probe_gpu(self.page)
        # The remote host can change between (re)connects -> a different GPU changes the
        # canvas identity. Surface it so the caller can rotate/quarantine the persona.
        if self.gpu_renderer and r and r != self.gpu_renderer:
            print("[backend] WARNING: GPU drift %r -> %r (canvas identity changed)" %
                  (self.gpu_renderer, r), flush=True)
        self.gpu_vendor, self.gpu_renderer = v, r
        await _warm(self.page)
        print("[backend] connected over CDP; render GPU=%r" % self.gpu_renderer, flush=True)

    async def start(self):
        await self._connect(self.get_cdp_url())

    async def ensure(self):
        alive = (self.browser is not None and self.browser.is_connected()
                 and self.page is not None and not self.page.is_closed())
        if alive:
            return
        try:
            if self.browser:
                await self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        print("[backend] CDP session gone -> reconnecting", flush=True)
        await self._connect(self.get_cdp_url())

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
        except Exception:  # noqa: BLE001
            pass


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
    def b(self):
        n = self.u32(); v = self.d[self.o:self.o + n]; self.o += n; return v


def enc_welcome(gpu_vendor, gpu_renderer):
    out = struct.pack("<I", WELCOME) + struct.pack("<I", 1)
    for field in ["clearcote-render-server/1", "Windows", gpu_vendor, gpu_renderer]:
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


# ---- WebGL op id -> JS (matches WebGLOp in the codec). objs[] maps bridge ids. ----
def webgl_op_to_js(op, ints, floats, s, data):
    def n(i, d=0): return ints[i] if i < len(ints) else d
    def f(i, d=0.0): return floats[i] if i < len(floats) else d
    # ---- object lifecycle (ints[0] = the client's stable bridge object id) ----
    if op == 1:   # kCreateBuffer
        return f"objs[{n(0)}]=gl.createBuffer();"
    if op == 2:   # kCreateShader (ints[1] = GLenum type)
        return f"objs[{n(0)}]=gl.createShader({n(1)});"
    if op == 3:   # kCreateProgram
        return f"objs[{n(0)}]=gl.createProgram();"
    if op == 4:   # kCreateTexture
        return f"objs[{n(0)}]=gl.createTexture();"
    # ---- program / shader ----
    if op == 10:  # kShaderSource (ints[0]=shader, s=source)
        return f"gl.shaderSource(objs[{n(0)}],{json.dumps(s)});"
    if op == 11:  # kCompileShader
        return f"gl.compileShader(objs[{n(0)}]);"
    if op == 12:  # kAttachShader (program, shader)
        return f"gl.attachShader(objs[{n(0)}],objs[{n(1)}]);"
    if op == 13:  # kBindAttribLocation (program, index, name)
        return f"gl.bindAttribLocation(objs[{n(0)}],{n(1)},{json.dumps(s)});"
    if op == 14:  # kLinkProgram
        return f"gl.linkProgram(objs[{n(0)}]);"
    if op == 15:  # kUseProgram (0 = null)
        return f"gl.useProgram({n(0)}?objs[{n(0)}]:null);"
    if op == 16:  # kGetUniformLocation (program, locId; s=name) -> objs[locId]
        return f"objs[{n(1)}]=gl.getUniformLocation(objs[{n(0)}],{json.dumps(s)});"
    # ---- uniforms (ints[0] = location bridge id, floats/ints carry values) ----
    if op == 40:  # kUniform1f
        return f"gl.uniform1f(objs[{n(0)}],{f(0)});"
    if op == 41:  # kUniform2f
        return f"gl.uniform2f(objs[{n(0)}],{f(0)},{f(1)});"
    if op == 42:  # kUniform3f
        return f"gl.uniform3f(objs[{n(0)}],{f(0)},{f(1)},{f(2)});"
    if op == 43:  # kUniform4f
        return f"gl.uniform4f(objs[{n(0)}],{f(0)},{f(1)},{f(2)},{f(3)});"
    if op == 44:  # kUniform1i (ints[1] = int value)
        return f"gl.uniform1i(objs[{n(0)}],{n(1)});"
    if op == 50:  # kUniformMatrix4fv (ints[1]=transpose, floats=16*count values)
        mat = ",".join(repr(x) for x in floats)
        return (f"gl.uniformMatrix4fv(objs[{n(0)}],"
                f"{'true' if n(1) else 'false'},[{mat}]);")
    # ---- buffers / vertex attribs ----
    if op == 20:  # kBindBuffer (target, buffer; 0 = null)
        return f"gl.bindBuffer({n(0)},{n(1)}?objs[{n(1)}]:null);"
    if op == 21:  # kBufferData (target, usage, binary blob)
        b64 = base64.b64encode(bytes(data)).decode()
        return (f"(function(){{var b=atob('{b64}');var u=new Uint8Array(b.length);"
                f"for(var i=0;i<b.length;i++)u[i]=b.charCodeAt(i);"
                f"gl.bufferData({n(0)},u,{n(1)});}})();")
    if op == 22:  # kBufferDataSize (target, size, usage)
        return f"gl.bufferData({n(0)},{n(1)},{n(2)});"
    if op == 30:  # kEnableVertexAttribArray
        return f"gl.enableVertexAttribArray({n(0)});"
    if op == 31:  # kDisableVertexAttribArray
        return f"gl.disableVertexAttribArray({n(0)});"
    if op == 32:  # kVertexAttribPointer (index,size,type,normalized,stride,offset)
        return (f"gl.vertexAttribPointer({n(0)},{n(1)},{n(2)},"
                f"{'true' if n(3) else 'false'},{n(4)},{n(5)});")
    # ---- textures ----
    if op == 60:  # kBindTexture (target, texture; 0 = null)
        return f"gl.bindTexture({n(0)},{n(1)}?objs[{n(1)}]:null);"
    if op == 61:  # kTexImage2D (target,level,internalformat,w,h,border,format,type; data)
        if data:
            b64 = base64.b64encode(bytes(data)).decode()
            return (f"(function(){{var b=atob('{b64}');var u=new Uint8Array(b.length);"
                    f"for(var i=0;i<b.length;i++)u[i]=b.charCodeAt(i);"
                    f"gl.texImage2D({n(0)},{n(1)},{n(2)},{n(3)},{n(4)},{n(5)},"
                    f"{n(6)},{n(7)},u);}})();")
        return (f"gl.texImage2D({n(0)},{n(1)},{n(2)},{n(3)},{n(4)},{n(5)},"
                f"{n(6)},{n(7)},null);")
    if op == 62:  # kTexParameteri (target, pname, param)
        return f"gl.texParameteri({n(0)},{n(1)},{n(2)});"
    if op == 63:  # kActiveTexture
        return f"gl.activeTexture({n(0)});"
    if op == 64:  # kGenerateMipmap
        return f"gl.generateMipmap({n(0)});"
    if op == 65:  # kTexParameterf (target, pname, paramf)
        return f"gl.texParameterf({n(0)},{n(1)},{f(0)});"
    if op == 66:  # kTexSubImage2D (target,level,xoff,yoff,w,h,format,type; data)
        b64 = base64.b64encode(bytes(data)).decode()
        return (f"(function(){{var b=atob('{b64}');var u=new Uint8Array(b.length);"
                f"for(var i=0;i<b.length;i++)u[i]=b.charCodeAt(i);"
                f"gl.texSubImage2D({n(0)},{n(1)},{n(2)},{n(3)},{n(4)},{n(5)},"
                f"{n(6)},{n(7)},u);}})();")
    # ---- fixed-function state ----
    if op == 80:  # kViewport
        return f"gl.viewport({n(0)},{n(1)},{n(2)},{n(3)});"
    if op == 81:  # kClearColor
        return f"gl.clearColor({f(0)},{f(1)},{f(2)},{f(3)});"
    if op == 82:  # kClear
        return f"gl.clear({n(0)});"
    if op == 83:  # kEnable
        return f"gl.enable({n(0)});"
    if op == 84:  # kDisable
        return f"gl.disable({n(0)});"
    if op == 85:  # kBlendFunc
        return f"gl.blendFunc({n(0)},{n(1)});"
    if op == 86:  # kDepthFunc
        return f"gl.depthFunc({n(0)});"
    if op == 87:  # kPixelStorei
        return f"gl.pixelStorei({n(0)},{n(1)});"
    if op == 88:  # kScissor
        return f"gl.scissor({n(0)},{n(1)},{n(2)},{n(3)});"
    # ---- draws ----
    if op == 90:  # kDrawArrays (mode, first, count)
        return f"gl.drawArrays({n(0)},{n(1)},{n(2)});"
    if op == 91:  # kDrawElements (mode, count, type, offset)
        return f"gl.drawElements({n(0)},{n(1)},{n(2)},{n(3)});"
    return ""


def build_webgl_js(cw, ch, ops, x, y, w, h):
    body = (f"const c=new OffscreenCanvas({cw},{ch});"
            + "const gl=c.getContext('webgl')||c.getContext('experimental-webgl');"
            + "if(!gl)return '';const objs={};"
            + "".join(ops)
            + f"const u=new Uint8Array({w}*{h}*4);"
            + f"gl.readPixels({x},{y},{w},{h},gl.RGBA,gl.UNSIGNED_BYTE,u);"
            + "let s='';const CH=16384;for(let i=0;i<u.length;i+=CH)"
            + "s+=String.fromCharCode.apply(null,u.subarray(i,i+CH));return btoa(s);")
    return "() => {" + body + "}"


async def handle_client(reader, writer, backend):
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
                await write_ws_frame(writer, enc_welcome(backend.gpu_vendor, backend.gpu_renderer))
            elif t == CREATE_CANVAS2D:
                cid, cw, ch = r.u32(), r.u32(), r.u32()
                canvases[cid] = {"w": max(cw, 1), "h": max(ch, 1), "ops": []}
                print("[server] CreateCanvas2D id=%d %dx%d" % (cid, cw, ch), flush=True)
            elif t == CANVAS2D_OP:
                cid, op = r.u32(), r.u32(); s = r.s(); args = [r.f64() for _ in range(r.u32())]
                cv = canvases.get(cid)
                if cv is not None:
                    cv["ops"].append(op_to_js(op, s, args))
            elif t == CREATE_WEBGL:
                cid, cw, ch, ctype = r.u32(), r.u32(), r.u32(), r.u32()
                canvases[cid] = {"type": "webgl", "w": max(cw, 1), "h": max(ch, 1), "ops": []}
                print("[server] CreateWebGL id=%d %dx%d type=%d" % (cid, cw, ch, ctype), flush=True)
            elif t == WEBGL_OP:
                cid, op = r.u32(), r.u32()
                ints = [r.i32() for _ in range(r.u32())]
                floats = [r.f64() for _ in range(r.u32())]
                s = r.s(); data = r.b()
                cv = canvases.get(cid)
                if cv is not None:
                    cv["ops"].append(webgl_op_to_js(op, ints, floats, s, data))
            elif t == GET_IMAGE_DATA:
                cid, x, y, w, h = r.u32(), r.i32(), r.i32(), r.u32(), r.u32()
                cv = canvases.get(cid, {"w": w, "h": h, "ops": []})
                if cv.get("type") == "webgl":
                    js = build_webgl_js(cv["w"], cv["h"], cv["ops"], x, y, w, h)
                else:
                    js = build_render_js(cv["w"], cv["h"], cv["ops"], x, y, w, h)
                async with backend.lock:
                    try:
                        await backend.ensure()
                        b64 = await backend.page.evaluate(js)
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
                async with backend.lock:
                    try:
                        await backend.ensure()
                        vals = await backend.page.evaluate(js)
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
    ap.add_argument("--backend", choices=["local", "cdp"], default="local")
    ap.add_argument("--port", type=int, default=9099)
    ap.add_argument("--host", default="127.0.0.1")
    # local backend
    ap.add_argument("--chrome", help="path to a clearcote chrome binary (--backend local)")
    ap.add_argument("--fingerprint", default="")
    # cdp backend
    ap.add_argument("--cdp-url", help="CDP/WebSocket endpoint of the render browser (--backend cdp)")
    args = ap.parse_args()

    async with async_playwright() as pw:
        if args.backend == "cdp":
            backend = RemoteCDPBackend(pw, args.cdp_url)
        else:
            if not args.chrome:
                sys.exit("--backend local requires --chrome <path>")
            backend = LocalBackend(pw, args.chrome, args.fingerprint)
        await backend.start()
        print("[server] backend=%s up; render GPU=%r; listening on ws://%s:%d" %
              (args.backend, backend.gpu_renderer, args.host, args.port), flush=True)
        srv = await asyncio.start_server(
            lambda r, w: handle_client(r, w, backend), args.host, args.port)
        try:
            async with srv:
                await srv.serve_forever()
        finally:
            await backend.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
