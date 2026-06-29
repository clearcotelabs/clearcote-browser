// Live render-backend coherence probe (#7) - the page-level counterpart to the launch-time heuristic
// in warnings. Reads what the page actually sees from WebGL and checks the two render-coherence tells
// a strict detector looks for: a software rasterizer (SwiftShader/llvmpipe/Mesa OffScreen - fatal on
// a "stealth" build), and an incoherent vendor/renderer GPU-family pair. It cannot read the real host
// GPU when the persona spoofs the unmasked strings (that's the point of the spoof) - it verifies the
// values the page is allowed to see are internally coherent and not a software fallback. For the
// deeper "do the pixels match the claimed GPU class" check, route paints through the canvas bridge.

import type { Page } from "playwright-core";

/** Probe JS - reads VENDOR/RENDERER + the unmasked pair via a throwaway WebGL context. */
const PROBE_JS = `() => {
  const out = { webgl: false, webgl2: false, vendor: "", renderer: "",
                unmaskedVendor: "", unmaskedRenderer: "", maxTextureSize: 0 };
  try {
    const c = document.createElement('canvas');
    const gl2 = c.getContext('webgl2');
    const gl = gl2 || c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return out;
    out.webgl = true;
    out.webgl2 = !!gl2;
    out.vendor = gl.getParameter(gl.VENDOR) || "";
    out.renderer = gl.getParameter(gl.RENDERER) || "";
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    if (dbg) {
      out.unmaskedVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) || "";
      out.unmaskedRenderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || "";
    }
    out.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE) || 0;
  } catch (e) { out.error = String(e); }
  return out;
}`;

const SOFTWARE_MARKERS = [
  "swiftshader", "google swiftshader", "llvmpipe", "softpipe",
  "mesa offscreen", "microsoft basic render", "software adapter",
];

const FAMILY_KEYS: [string, string][] = [
  ["nvidia", "nvidia"], ["geforce", "nvidia"], ["rtx", "nvidia"], ["gtx", "nvidia"], ["quadro", "nvidia"],
  ["radeon", "amd"], ["amd", "amd"], ["ati ", "amd"],
  ["intel", "intel"], ["iris", "intel"], ["uhd graphics", "intel"], ["hd graphics", "intel"],
  ["apple", "apple"], ["m1", "apple"], ["m2", "apple"], ["m3", "apple"], ["m4", "apple"],
  ["mali", "mali"], ["adreno", "adreno"], ["powervr", "powervr"],
];

/** Best-effort GPU family from a vendor/renderer string ('' if unknown). */
export function gpuFamily(s: string | undefined): string {
  const l = (s || "").toLowerCase();
  for (const [key, fam] of FAMILY_KEYS) if (l.includes(key)) return fam;
  return "";
}

export interface RenderInfo {
  webgl?: boolean;
  webgl2?: boolean;
  vendor?: string;
  renderer?: string;
  unmaskedVendor?: string;
  unmaskedRenderer?: string;
  maxTextureSize?: number;
}

export interface RenderVerdict {
  vendor: string;
  renderer: string;
  webgl: boolean;
  webgl2: boolean;
  maxTextureSize: number;
  softwareSuspected: boolean;
  coherent: boolean;
  warnings: string[];
}

/** Pure analysis of a probe result -> coherence verdict (unit-testable, no Playwright). */
export function evaluateRenderInfo(info: RenderInfo, claimedGpu?: string): RenderVerdict {
  const renderer = info.unmaskedRenderer || info.renderer || "";
  const vendor = info.unmaskedVendor || info.vendor || "";
  const rl = renderer.toLowerCase();
  const vl = vendor.toLowerCase();
  const warnings: string[] = [];

  const hasWebgl = !!info.webgl;
  if (!hasWebgl) {
    warnings.push(
      "WebGL is unavailable - a hard tell for a real desktop browser (only headless or locked-down setups disable it)."
    );
  }

  const software = SOFTWARE_MARKERS.some((m) => rl.includes(m) || vl.includes(m));
  if (software) {
    warnings.push(
      `software rasterizer detected in the WebGL renderer (${JSON.stringify(renderer)}) - a definitive ` +
        "headless/no-GPU tell. Enable the canvas bridge (canvasBridge: ...) or run headed on a machine with a real GPU."
    );
  }

  const rfam = gpuFamily(rl);
  const vfam = gpuFamily(vl);
  if (rfam && vfam && rfam !== vfam) {
    warnings.push(
      `WebGL vendor and renderer disagree on GPU family (vendor~${vfam}, renderer~${rfam}) - an incoherent persona.`
    );
  }

  if (claimedGpu) {
    const cfam = gpuFamily(claimedGpu);
    if (cfam && rfam && cfam !== rfam) {
      warnings.push(
        `the claimed GPU (${JSON.stringify(claimedGpu)}, family ~${cfam}) does not match the WebGL renderer family (~${rfam}).`
      );
    }
  }

  const coherent =
    hasWebgl && !software && !warnings.some((w) => w.includes("disagree") || w.includes("does not match"));
  return {
    vendor,
    renderer,
    webgl: hasWebgl,
    webgl2: !!info.webgl2,
    maxTextureSize: info.maxTextureSize || 0,
    softwareSuspected: software,
    coherent,
    warnings,
  };
}

/**
 * Probe a live Playwright `page` for render-backend coherence (#7). Returns the vendor/renderer the
 * page actually sees, `softwareSuspected` (a SwiftShader/llvmpipe fallback is a fatal tell),
 * `coherent`, and human-readable `warnings`. Pass `claimedGpu` to also assert the rendered family.
 *
 * @example
 * const br = await clearcote.launch({ fingerprint: "77" });
 * const page = await br.newPage(); await page.goto("about:blank");
 * const verdict = await checkRenderCoherence(page);
 * if (!verdict.coherent) console.warn(verdict.warnings);
 */
export async function checkRenderCoherence(page: Page, claimedGpu?: string): Promise<RenderVerdict> {
  const info = (await page.evaluate(PROBE_JS)) as RenderInfo;
  return evaluateRenderInfo(info, claimedGpu);
}
