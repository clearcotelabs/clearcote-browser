/*
 * clearcote fingerprint collector
 * ================================
 * Run this in a REAL Chrome on a donor machine to capture an exhaustive, importable
 * fingerprint profile. `collectFingerprint()` returns a Promise<object> matching the
 * clearcote-profile schema (see README.md) — a SUPERSET of the Vinyzu chrome-fingerprints
 * record (every WebGL param, the full Web Audio constant table, voices, fonts, codecs, css,
 * webrtc, webgpu) PLUS the fields that dataset omits (canvas metadata, performance.memory,
 * maxTouchPoints, navigator.connection, mediaDevices, battery, permissions, Intl, Math,
 * clientRects). Nothing JS-observable is intentionally left behind.
 *
 * Network-layer fields (HTTP header order, TLS/HTTP2) cannot be read from JS — the hosting
 * page (collect.html) POSTs this object to a collector endpoint that stitches them in.
 *
 * SCHEMA_VERSION bumps when the captured shape changes.
 */
(function (global) {
  "use strict";
  const SCHEMA_VERSION = 1;

  const safe = (fn, dflt = null) => { try { const v = fn(); return v === undefined ? dflt : v; } catch (e) { return dflt; } };
  const safeAsync = async (fn, dflt = null) => { try { const v = await fn(); return v === undefined ? dflt : v; } catch (e) { return dflt; } };

  // ---- navigator -----------------------------------------------------------
  async function collectNavigator() {
    const n = navigator;
    const o = {
      user_agent: safe(() => n.userAgent),
      app_version: safe(() => n.appVersion),
      app_codename: safe(() => n.appCodeName),
      app_name: safe(() => n.appName),
      product: safe(() => n.product),
      product_sub: safe(() => n.productSub),
      vendor: safe(() => n.vendor),
      vendor_sub: safe(() => n.vendorSub),
      platform: safe(() => n.platform),
      language: safe(() => n.language),
      languages: safe(() => (n.languages || []).slice()),
      hardware_concurrency: safe(() => n.hardwareConcurrency),
      device_memory: safe(() => n.deviceMemory),
      max_touch_points: safe(() => n.maxTouchPoints),
      do_not_track: safe(() => n.doNotTrack),
      cookie_enabled: safe(() => n.cookieEnabled),
      pdf_viewer_enabled: safe(() => n.pdfViewerEnabled),
      webdriver: safe(() => n.webdriver),
      oscpu: safe(() => n.oscpu),
      uadata: null,
    };
    // UA Client Hints — low + ALL high-entropy values
    if (n.userAgentData) {
      o.uadata = {
        brands: safe(() => n.userAgentData.brands.map((b) => ({ brand: b.brand, version: b.version }))),
        mobile: safe(() => n.userAgentData.mobile),
        platform: safe(() => n.userAgentData.platform),
      };
      o.uadata.high_entropy = await safeAsync(() =>
        n.userAgentData.getHighEntropyValues([
          "architecture", "bitness", "model", "platformVersion",
          "uaFullVersion", "fullVersionList", "wow64", "formFactors",
        ])
      );
    }
    return o;
  }

  // ---- screen / window geometry -------------------------------------------
  function collectScreen() {
    const s = screen;
    return {
      width: safe(() => s.width), height: safe(() => s.height),
      avail_width: safe(() => s.availWidth), avail_height: safe(() => s.availHeight),
      avail_left: safe(() => s.availLeft), avail_top: safe(() => s.availTop),
      color_depth: safe(() => s.colorDepth), pixel_depth: safe(() => s.pixelDepth),
      is_extended: safe(() => s.isExtended),
      orientation_type: safe(() => s.orientation && s.orientation.type),
      orientation_angle: safe(() => s.orientation && s.orientation.angle),
      device_pixel_ratio: safe(() => global.devicePixelRatio),
      inner_width: safe(() => global.innerWidth), inner_height: safe(() => global.innerHeight),
      outer_width: safe(() => global.outerWidth), outer_height: safe(() => global.outerHeight),
      screen_x: safe(() => global.screenX), screen_y: safe(() => global.screenY),
    };
  }

  // ---- plugins / mimeTypes -------------------------------------------------
  function collectPlugins() {
    return safe(() => Array.from(navigator.plugins).map((p) => ({
      name: p.name, filename: p.filename, description: p.description,
      mimes: Array.from(p).map((m) => ({ type: m.type, suffixes: m.suffixes, description: m.description })),
    })), []);
  }
  function collectMimeTypes() {
    return safe(() => Array.from(navigator.mimeTypes).map((m) => ({ type: m.type, suffixes: m.suffixes, description: m.description })), []);
  }

  // ---- WebGL (1 + 2): full param table + shader precision + extensions -----
  // getParameter pnames that carry fingerprint entropy (superset of the dataset's set).
  const GL_PNAMES = [
    "ALIASED_LINE_WIDTH_RANGE", "ALIASED_POINT_SIZE_RANGE", "ALPHA_BITS", "BLUE_BITS",
    "DEPTH_BITS", "GREEN_BITS", "RED_BITS", "STENCIL_BITS", "SUBPIXEL_BITS",
    "MAX_COMBINED_TEXTURE_IMAGE_UNITS", "MAX_CUBE_MAP_TEXTURE_SIZE",
    "MAX_FRAGMENT_UNIFORM_VECTORS", "MAX_RENDERBUFFER_SIZE", "MAX_TEXTURE_IMAGE_UNITS",
    "MAX_TEXTURE_SIZE", "MAX_VARYING_VECTORS", "MAX_VERTEX_ATTRIBS",
    "MAX_VERTEX_TEXTURE_IMAGE_UNITS", "MAX_VERTEX_UNIFORM_VECTORS", "MAX_VIEWPORT_DIMS",
    "SAMPLE_BUFFERS", "SAMPLES", "STENCIL_BACK_VALUE_MASK", "STENCIL_BACK_WRITEMASK",
    "STENCIL_VALUE_MASK", "STENCIL_WRITEMASK",
  ];
  const GL2_PNAMES = [
    "MAX_3D_TEXTURE_SIZE", "MAX_ARRAY_TEXTURE_LAYERS", "MAX_COLOR_ATTACHMENTS",
    "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS", "MAX_COMBINED_UNIFORM_BLOCKS",
    "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS", "MAX_DRAW_BUFFERS", "MAX_ELEMENT_INDEX",
    "MAX_ELEMENTS_INDICES", "MAX_ELEMENTS_VERTICES", "MAX_FRAGMENT_INPUT_COMPONENTS",
    "MAX_FRAGMENT_UNIFORM_BLOCKS", "MAX_FRAGMENT_UNIFORM_COMPONENTS", "MAX_PROGRAM_TEXEL_OFFSET",
    "MIN_PROGRAM_TEXEL_OFFSET", "MAX_SAMPLES", "MAX_SERVER_WAIT_TIMEOUT", "MAX_TEXTURE_LOD_BIAS",
    "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS", "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS",
    "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS", "MAX_UNIFORM_BLOCK_SIZE",
    "MAX_UNIFORM_BUFFER_BINDINGS", "MAX_VARYING_COMPONENTS", "MAX_VERTEX_OUTPUT_COMPONENTS",
    "MAX_VERTEX_UNIFORM_BLOCKS", "MAX_VERTEX_UNIFORM_COMPONENTS", "UNIFORM_BUFFER_OFFSET_ALIGNMENT",
  ];
  const SHADER_TYPES = ["VERTEX_SHADER", "FRAGMENT_SHADER"];
  const PRECISIONS = ["HIGH_FLOAT", "MEDIUM_FLOAT", "LOW_FLOAT", "HIGH_INT", "MEDIUM_INT", "LOW_INT"];

  function captureGLContext(gl, pnames) {
    const out = { parameters: {}, shader_precision: {}, extensions: null, context_attributes: null, debug: {} };
    const arr = (v) => (v && v.length !== undefined && typeof v !== "string") ? Array.from(v) : v;
    for (const name of pnames) {
      if (gl[name] === undefined) continue;
      out.parameters[name] = safe(() => arr(gl.getParameter(gl[name])));
    }
    // unmasked vendor/renderer via the debug extension
    const dbg = safe(() => gl.getExtension("WEBGL_debug_renderer_info"));
    out.debug = {
      VENDOR: safe(() => gl.getParameter(gl.VENDOR)),
      RENDERER: safe(() => gl.getParameter(gl.RENDERER)),
      VERSION: safe(() => gl.getParameter(gl.VERSION)),
      SHADING_LANGUAGE_VERSION: safe(() => gl.getParameter(gl.SHADING_LANGUAGE_VERSION)),
      UNMASKED_VENDOR_WEBGL: safe(() => dbg && gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL)),
      UNMASKED_RENDERER_WEBGL: safe(() => dbg && gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)),
    };
    // max anisotropy
    const aniso = safe(() => gl.getExtension("EXT_texture_filter_anisotropic"));
    out.parameters.MAX_TEXTURE_MAX_ANISOTROPY_EXT = safe(() => aniso && gl.getParameter(aniso.MAX_TEXTURE_MAX_ANISOTROPY_EXT));
    // shader precision formats
    for (const st of SHADER_TYPES) {
      for (const pr of PRECISIONS) {
        const f = safe(() => gl.getShaderPrecisionFormat(gl[st], gl[pr]));
        if (f) out.shader_precision[st + ":" + pr] = { rangeMin: f.rangeMin, rangeMax: f.rangeMax, precision: f.precision };
      }
    }
    out.extensions = safe(() => gl.getSupportedExtensions(), []);
    out.context_attributes = safe(() => gl.getContextAttributes());
    out.drawing_buffer = { width: safe(() => gl.drawingBufferWidth), height: safe(() => gl.drawingBufferHeight) };
    return out;
  }

  function collectWebGL() {
    const c = document.createElement("canvas");
    const gl1 = safe(() => c.getContext("webgl") || c.getContext("experimental-webgl"));
    const c2 = document.createElement("canvas");
    const gl2 = safe(() => c2.getContext("webgl2"));
    return {
      webgl1: gl1 ? captureGLContext(gl1, GL_PNAMES) : null,
      webgl2: gl2 ? captureGLContext(gl2, GL_PNAMES.concat(GL2_PNAMES)) : null,
    };
  }

  // ---- WebGPU --------------------------------------------------------------
  async function collectWebGPU() {
    if (!global.navigator.gpu) return null;
    return await safeAsync(async () => {
      const adapter = await navigator.gpu.requestAdapter();
      if (!adapter) return { available: false };
      const info = await safeAsync(() => adapter.requestAdapterInfo ? adapter.requestAdapterInfo() : adapter.info, {});
      const limits = {};
      if (adapter.limits) for (const k in adapter.limits) limits[k] = adapter.limits[k];
      return {
        available: true,
        is_fallback_adapter: safe(() => adapter.isFallbackAdapter),
        features: safe(() => Array.from(adapter.features || [])),
        info: { vendor: safe(() => info.vendor), architecture: safe(() => info.architecture), device: safe(() => info.device), description: safe(() => info.description) },
        limits: limits,
        preferred_canvas_format: safe(() => navigator.gpu.getPreferredCanvasFormat && navigator.gpu.getPreferredCanvasFormat()),
      };
    });
  }

  // ---- WebRTC capabilities -------------------------------------------------
  function collectWebRTC() {
    const cap = (kind, dir) => safe(() => {
      const fn = dir === "sender" ? RTCRtpSender.getCapabilities : RTCRtpReceiver.getCapabilities;
      const c = fn(kind);
      return { codecs: c.codecs, headerExtensions: c.headerExtensions };
    });
    return {
      sender: { audio: cap("audio", "sender"), video: cap("video", "sender") },
      receiver: { audio: cap("audio", "receiver"), video: cap("video", "receiver") },
    };
  }

  // ---- fonts (measureText width probe over a broad list) -------------------
  const FONT_PROBE = ["monospace", "sans-serif", "serif"];
  const FONT_LIST = ["Arial","Arial Black","Arial Narrow","Calibri","Cambria","Cambria Math","Comic Sans MS","Consolas","Courier","Courier New","Georgia","Helvetica","Impact","Lucida Console","Lucida Sans Unicode","Microsoft Sans Serif","Palatino Linotype","Segoe Print","Segoe Script","Segoe UI","Segoe UI Emoji","Segoe UI Symbol","Tahoma","Times","Times New Roman","Trebuchet MS","Verdana","Webdings","Wingdings","MS Gothic","MS PGothic","MS UI Gothic","MS Mincho","MS PMincho","Meiryo","Yu Gothic","Malgun Gothic","Gulim","Batang","SimSun","SimHei","Microsoft YaHei","NSimSun","PMingLiU","MingLiU","Cantarell","DejaVu Sans","Liberation Sans","Ubuntu","Roboto","Noto Sans","Franklin Gothic Medium","Gabriola","Candara","Constantia","Corbel","Ebrima","Gadugi","Javanese Text","Leelawadee UI","Marlett","MV Boli","Myanmar Text","Nirmala UI","Sitka","Sylfaen","Symbol"];
  function collectFonts() {
    const baseline = {};
    const span = document.createElement("span");
    span.style.cssText = "position:absolute;left:-9999px;font-size:72px;";
    span.textContent = "mmmmmmmmmmlli WwWwWw 0123456789";
    document.body.appendChild(span);
    for (const b of FONT_PROBE) { span.style.fontFamily = b; baseline[b] = { w: span.offsetWidth, h: span.offsetHeight }; }
    const detected = [];
    for (const f of FONT_LIST) {
      let present = false;
      for (const b of FONT_PROBE) {
        span.style.fontFamily = "'" + f + "'," + b;
        if (span.offsetWidth !== baseline[b].w || span.offsetHeight !== baseline[b].h) { present = true; break; }
      }
      if (present) detected.push(f);
    }
    document.body.removeChild(span);
    return { detected: detected, probed: FONT_LIST.length };
  }

  // ---- media codec support matrix ------------------------------------------
  const VIDEO_TYPES = ['video/mp4; codecs="avc1.42E01E"','video/mp4; codecs="hev1.1.6.L93.B0"','video/webm; codecs="vp8"','video/webm; codecs="vp9"','video/mp4; codecs="av01.0.05M.08"','video/ogg; codecs="theora"'];
  const AUDIO_TYPES = ['audio/mp4; codecs="mp4a.40.2"','audio/mpeg','audio/ogg; codecs="vorbis"','audio/ogg; codecs="opus"','audio/wav; codecs="1"','audio/webm; codecs="vorbis"','audio/flac'];
  async function collectCodecs() {
    const v = document.createElement("video"), a = document.createElement("audio");
    const out = { canPlayType: {}, isTypeSupported: {}, decodingInfo: {} };
    for (const t of VIDEO_TYPES) {
      out.canPlayType[t] = safe(() => v.canPlayType(t));
      out.isTypeSupported[t] = safe(() => global.MediaSource && MediaSource.isTypeSupported(t));
      out.decodingInfo[t] = await safeAsync(async () => {
        const r = await navigator.mediaCapabilities.decodingInfo({ type: "file", video: { contentType: t, width: 1920, height: 1080, bitrate: 4000000, framerate: 30 } });
        return { supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient };
      });
    }
    for (const t of AUDIO_TYPES) {
      out.canPlayType[t] = safe(() => a.canPlayType(t));
      out.decodingInfo[t] = await safeAsync(async () => {
        const r = await navigator.mediaCapabilities.decodingInfo({ type: "file", audio: { contentType: t } });
        return { supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient };
      });
    }
    return out;
  }

  // ---- CSS @media query results --------------------------------------------
  function collectCSS() {
    const mm = (q) => safe(() => matchMedia(q).matches);
    const css = {
      "any-hover:hover": mm("(any-hover: hover)"), "any-hover:none": mm("(any-hover: none)"),
      "any-pointer:fine": mm("(any-pointer: fine)"), "any-pointer:coarse": mm("(any-pointer: coarse)"), "any-pointer:none": mm("(any-pointer: none)"),
      "hover:hover": mm("(hover: hover)"), "hover:none": mm("(hover: none)"),
      "pointer:fine": mm("(pointer: fine)"), "pointer:coarse": mm("(pointer: coarse)"), "pointer:none": mm("(pointer: none)"),
      "orientation:landscape": mm("(orientation: landscape)"), "orientation:portrait": mm("(orientation: portrait)"),
      "prefers-color-scheme:dark": mm("(prefers-color-scheme: dark)"), "prefers-color-scheme:light": mm("(prefers-color-scheme: light)"),
      "prefers-reduced-motion:reduce": mm("(prefers-reduced-motion: reduce)"),
      "prefers-contrast:more": mm("(prefers-contrast: more)"),
      "update:fast": mm("(update: fast)"), "overflow-block:scroll": mm("(overflow-block: scroll)"),
      "grid:0": mm("(grid: 0)"),
    };
    for (const g of ["srgb", "p3", "rec2020"]) css["color-gamut:" + g] = mm("(color-gamut: " + g + ")");
    css["color"] = safe(() => { for (let i = 24; i >= 1; i--) if (matchMedia("(color: " + i + ")").matches) return i; return 0; });
    css["color-index"] = safe(() => matchMedia("(min-color-index: 1)").matches ? 1 : 0);
    css["monochrome"] = safe(() => matchMedia("(min-monochrome: 1)").matches ? 1 : 0);
    css["device-width"] = safe(() => screen.width); css["device-height"] = safe(() => screen.height);
    css["resolution_dppx"] = safe(() => global.devicePixelRatio);
    return css;
  }

  // ---- Web Audio constant table (every node's defaultValue/min/max) --------
  function collectAudio() {
    const out = {};
    const OAC = global.OfflineAudioContext || global.webkitOfflineAudioContext;
    if (!OAC) return out;
    const ctx = safe(() => new OAC(1, 44100, 44100));
    if (!ctx) return out;
    out.BaseAudioContextSampleRate = safe(() => ctx.sampleRate);
    out.AudioContextBaseLatency = safe(() => ctx.baseLatency);
    out.AudioContextOutputLatency = safe(() => ctx.outputLatency);
    out.AudioDestinationNodeMaxChannelCount = safe(() => ctx.destination.maxChannelCount);
    // record every AudioParam on a node as <NodeName><ParamName>{Default,Min,Max}Value
    const param = (node, label, name) => {
      const p = node[name];
      if (!p || p.defaultValue === undefined) return;
      out[label + cap(name) + "DefaultValue"] = p.defaultValue;
      out[label + cap(name) + "MinValue"] = p.minValue;
      out[label + cap(name) + "MaxValue"] = p.maxValue;
    };
    const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
    safe(() => { const a = ctx.createAnalyser(); out.AnalyzerNodeFftSize = a.fftSize; out.AnalyzerNodeFrequencyBinCount = a.frequencyBinCount; out.AnalyzerNodeMinDecibels = a.minDecibels; out.AnalyzerNodeMaxDecibels = a.maxDecibels; out.AnalyzerNodeSmoothingTimeConstant = a.smoothingTimeConstant; });
    safe(() => { const b = ctx.createBiquadFilter(); param(b, "BiquadFilterNode", "frequency"); param(b, "BiquadFilterNode", "detune"); param(b, "BiquadFilterNode", "Q"); param(b, "BiquadFilterNode", "gain"); out.BiquadFilterNodeType = b.type; });
    safe(() => { const s = ctx.createBufferSource(); param(s, "AudioBufferSourceNode", "detune"); param(s, "AudioBufferSourceNode", "playbackRate"); });
    safe(() => { const c = ctx.createConstantSource(); param(c, "ConstantSourceNode", "offset"); });
    safe(() => { const d = ctx.createDelay(); param(d, "DelayNode", "delayTime"); });
    safe(() => { const d = ctx.createDynamicsCompressor(); param(d, "DynamicsCompressorNode", "threshold"); param(d, "DynamicsCompressorNode", "knee"); param(d, "DynamicsCompressorNode", "ratio"); param(d, "DynamicsCompressorNode", "attack"); param(d, "DynamicsCompressorNode", "release"); out.DynamicsCompressorNodeReductionDefaultValue = d.reduction; });
    safe(() => { const g = ctx.createGain(); param(g, "GainNode", "gain"); });
    safe(() => { const o = ctx.createOscillator(); param(o, "OscillatorNode", "frequency"); param(o, "OscillatorNode", "detune"); out.OscillatorNodeType = o.type; });
    safe(() => { const p = ctx.createStereoPanner(); param(p, "StereoPannerNode", "pan"); });
    safe(() => { const p = ctx.createPanner(); ["orientationX","orientationY","orientationZ","positionX","positionY","positionZ"].forEach((nm) => param(p, "PannerNode", nm)); });
    safe(() => { const l = ctx.listener; if (l) ["positionX","positionY","positionZ","forwardX","forwardY","forwardZ","upX","upY","upZ"].forEach((nm) => param(l, "AudioListener", nm)); });
    return out;
  }

  // ---- speech voices (await voiceschanged) ---------------------------------
  function collectVoices() {
    return new Promise((res) => {
      const get = () => safe(() => speechSynthesis.getVoices().map((v) => ({ voice_uri: v.voiceURI, name: v.name, lang: v.lang, local_service: v.localService, default: v.default })), []);
      let v = get();
      if (v.length) return res(v);
      let done = false;
      const t = setTimeout(() => { if (!done) { done = true; res(get()); } }, 2500);
      try { speechSynthesis.onvoiceschanged = () => { if (!done) { done = true; clearTimeout(t); res(get()); } }; } catch (e) { res(get()); }
    });
  }

  // ---- keyboard layout map -------------------------------------------------
  async function collectKeyboard() {
    return await safeAsync(async () => {
      const m = await navigator.keyboard.getLayoutMap();
      const o = {}; m.forEach((val, code) => { o[code] = val; }); return o;
    });
  }

  // ---- performance.memory --------------------------------------------------
  function collectPerfMemory() {
    return safe(() => performance.memory ? { jsHeapSizeLimit: performance.memory.jsHeapSizeLimit, totalJSHeapSize: performance.memory.totalJSHeapSize, usedJSHeapSize: performance.memory.usedJSHeapSize } : null);
  }

  // ---- navigator.connection ------------------------------------------------
  function collectConnection() {
    const c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    return c ? { effectiveType: safe(() => c.effectiveType), rtt: safe(() => c.rtt), downlink: safe(() => c.downlink), saveData: safe(() => c.saveData), type: safe(() => c.type) } : null;
  }

  // ---- mediaDevices --------------------------------------------------------
  async function collectMediaDevices() {
    return await safeAsync(async () => {
      const ds = await navigator.mediaDevices.enumerateDevices();
      return ds.map((d) => ({ kind: d.kind, hasLabel: !!d.label }));
    });
  }

  // ---- battery -------------------------------------------------------------
  async function collectBattery() {
    return await safeAsync(async () => {
      const b = await navigator.getBattery();
      const j = (v) => (v === Infinity ? "Infinity" : (typeof v === "number" && v !== v ? null : v)); // JSON-safe (Infinity/NaN)
      return { charging: b.charging, level: b.level, chargingTime: j(b.chargingTime), dischargingTime: j(b.dischargingTime) };
    });
  }

  // ---- permissions ---------------------------------------------------------
  async function collectPermissions() {
    const names = ["geolocation", "notifications", "camera", "microphone", "persistent-storage", "midi", "clipboard-read"];
    const o = {};
    for (const name of names) o[name] = await safeAsync(async () => (await navigator.permissions.query({ name: name })).state);
    o.notification_permission = safe(() => global.Notification && Notification.permission);
    return o;
  }

  // ---- Intl / timezone -----------------------------------------------------
  function collectIntl() {
    const dt = safe(() => Intl.DateTimeFormat().resolvedOptions(), {});
    return {
      timeZone: dt.timeZone, locale: dt.locale, calendar: dt.calendar, numberingSystem: dt.numberingSystem,
      hourCycle: dt.hourCycle, timezone_offset: safe(() => new Date().getTimezoneOffset()),
      date_string: safe(() => new Date(0).toString()),
      number_format: safe(() => new Intl.NumberFormat().format(1234567.89)),
    };
  }

  // ---- Math precision vector -----------------------------------------------
  function collectMath() {
    const fns = { acos: Math.acos(0.123), asin: Math.asin(0.123), atan: Math.atan(2), atanh: Math.atanh(0.5), cbrt: Math.cbrt(100), cos: Math.cos(1e10), cosh: Math.cosh(1), exp: Math.exp(1), expm1: Math.expm1(1), log: Math.log(1000), log1p: Math.log1p(10), sin: Math.sin(1e10), sinh: Math.sinh(1), sqrt: Math.sqrt(2), tan: Math.tan(1e10), tanh: Math.tanh(1), pow: Math.pow(Math.PI, -100) };
    return fns;
  }

  // ---- canvas metadata (reference hashes — NOT for replay; render-dependent)
  function collectCanvas() {
    const out = {};
    out.note = "Reference hashes only; canvas is render-dependent and is handled by farbling, not static replay.";
    safe(() => {
      const c = document.createElement("canvas"); c.width = 280; c.height = 60;
      const x = c.getContext("2d"); x.textBaseline = "top"; x.font = "14px 'Arial'";
      x.fillStyle = "#f60"; x.fillRect(1, 1, 100, 20); x.fillStyle = "#069"; x.fillText("clearcote fingerprint 😃", 2, 15);
      const data = c.toDataURL(); let h = 0; for (let i = 0; i < data.length; i++) h = (h * 31 + data.charCodeAt(i)) >>> 0;
      out.toDataURL_hash = h >>> 0;
      out.text_metrics = (() => { const m = x.measureText("clearcote MmWw 0123456789"); return { width: m.width, actualBoundingBoxAscent: m.actualBoundingBoxAscent, actualBoundingBoxDescent: m.actualBoundingBoxDescent, fontBoundingBoxAscent: m.fontBoundingBoxAscent }; })();
    });
    return out;
  }

  // ---- clientRects ---------------------------------------------------------
  function collectClientRects() {
    return safe(() => {
      const d = document.createElement("div");
      d.style.cssText = "position:absolute;left:13.37px;top:7.77px;width:50.5px;height:20.25px;transform:rotate(3deg);";
      document.body.appendChild(d); const r = d.getBoundingClientRect();
      document.body.removeChild(d);
      return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, left: r.left };
    });
  }

  // ---- top-level orchestrator ----------------------------------------------
  async function collectFingerprint() {
    const [navigatorData, webgpu, voices, keyboard, codecs, mediaDevices, battery, permissions] = await Promise.all([
      collectNavigator(), collectWebGPU(), collectVoices(), collectKeyboard(),
      collectCodecs(), collectMediaDevices(), collectBattery(), collectPermissions(),
    ]);
    return {
      meta: {
        schema_version: SCHEMA_VERSION,
        captured_at: safe(() => new Date().toISOString()),
        href: safe(() => location.href),
        chrome_version: safe(() => (navigator.userAgent.match(/Chrome\/([0-9.]+)/) || [])[1] || null),
        note: "JS-observable layer. HTTP header order + TLS/HTTP2 are stitched in server-side by the collector endpoint (see README).",
      },
      navigator: navigatorData,
      screen: collectScreen(),
      hardware_concurrency: safe(() => navigator.hardwareConcurrency),
      device_memory: safe(() => navigator.deviceMemory),
      max_touch_points: safe(() => navigator.maxTouchPoints),
      do_not_track: safe(() => navigator.doNotTrack),
      plugins: collectPlugins(),
      mime_types: collectMimeTypes(),
      speech: voices,
      webgl: collectWebGL(),
      webgpu: webgpu,
      webrtc: collectWebRTC(),
      fonts: collectFonts(),
      codecs: codecs,
      css: collectCSS(),
      audio: collectAudio(),
      perf_memory: collectPerfMemory(),
      connection: collectConnection(),
      media_devices: mediaDevices,
      battery: battery,
      permissions: permissions,
      intl: collectIntl(),
      math: collectMath(),
      canvas: collectCanvas(),
      client_rects: collectClientRects(),
      keyboard: keyboard,
      network: null, // filled by the collector endpoint (header order + TLS/HTTP2)
    };
  }

  global.collectFingerprint = collectFingerprint;
  if (typeof module !== "undefined" && module.exports) module.exports = { collectFingerprint };
})(typeof window !== "undefined" ? window : globalThis);
