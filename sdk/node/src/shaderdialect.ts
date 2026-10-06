/**
 * Optional HLSL shader dialect for a Windows persona on a non-Windows host.
 *
 * `WEBGL_debug_shaders.getTranslatedShaderSource()` returns whatever ANGLE's active backend
 * produced. A Windows persona advertises a Direct3D11 renderer, but on a Linux host the Vulkan
 * backend answers with a SPIR-V dump, so the renderer string and the dialect beside it contradict
 * each other. `shaderDialect: "hlsl"` makes the engine re-translate the shader to HLSL for that
 * query alone — rendering is untouched, and the result is byte-identical to what the Windows build
 * reports.
 *
 * ON BY DEFAULT for a Windows claim on a non-Windows host (the Docker entrypoint has done this for
 * CC_PLATFORM=windows since 0.31). Measured 2026-10-06 on a GPU-less Linux host with r30: a Windows
 * persona answered the query with SwiftShader's SPIR-V text next to "Direct3D11"; with the dialect
 * set it answered in HLSL. A shader the HLSL translator rejects falls back to the backend's own
 * output for that shader -- the state every launch was in before, so the default cannot make a page
 * see anything worse. Pass `shaderDialect: false` to turn it off. On a Windows host the D3D11
 * backend already answers in HLSL and nothing is set; a Linux or macOS claim never gets HLSL.
 *
 * Delivered as an environment variable because the code lives in the GPU process, which does not
 * receive the fingerprint switches.
 *
 * Requires a PRO engine built with the option (151 r15+); older binaries ignore the variable.
 */

export const SHADER_DIALECT_ENV = "CLEARCOTE_SHADER_DIALECT";

/** The dialects the engine understands. Anything else is a typo, not a feature. */
export type ShaderDialect = "hlsl";

const VALID: readonly string[] = ["hlsl"];

const OFF: readonly string[] = ["", "0", "off", "false", "no", "none"];

/** The dialect a launch gets when the caller did not choose one: `"hlsl"` when the built command
 * line claims Windows (`--fingerprint-platform=windows`, the last one wins) and the host is not
 * Windows, else undefined. */
export function defaultShaderDialect(args?: readonly string[], hostPlatform: string = process.platform): ShaderDialect | undefined {
  if (hostPlatform === "win32") return undefined;
  let claimed: string | undefined;
  for (const a of args ?? []) {
    if (typeof a === "string" && a.startsWith("--fingerprint-platform=")) {
      claimed = a.slice("--fingerprint-platform=".length).trim().toLowerCase();
    }
  }
  return claimed === "windows" ? "hlsl" : undefined;
}

/**
 * Fold `CLEARCOTE_SHADER_DIALECT` into a launch env.
 *
 * `dialect` undefined -> the default for this claim/host ({@link defaultShaderDialect} over `args`);
 * `false` or an "off" spelling -> nothing. Returns `baseEnv` untouched when nothing applies —
 * including the plain no-claim case, so the default Playwright env is preserved rather than being
 * replaced by a copy of `process.env`. The default never overrides a variable the caller exported.
 *
 * Throws on an unknown dialect rather than ignoring it: a typo would otherwise look like it worked
 * while the engine kept reporting the honest dialect.
 */
export function withShaderDialect(
  dialect: string | false | undefined,
  baseEnv: Record<string, string | undefined> | undefined,
  args?: readonly string[],
  hostPlatform: string = process.platform,
): Record<string, string | undefined> | undefined {
  let value: string | undefined;
  if (dialect === undefined) {
    value = defaultShaderDialect(args, hostPlatform);
    if (!value) return baseEnv;
    if ((baseEnv ?? process.env)[SHADER_DIALECT_ENV] !== undefined) return baseEnv;
  } else {
    if (dialect === false || OFF.includes(String(dialect).trim().toLowerCase())) return baseEnv;
    value = String(dialect).trim().toLowerCase();
    if (!VALID.includes(value)) {
      throw new Error(
        `shaderDialect must be one of ${VALID.join(", ")} (got ${JSON.stringify(dialect)})`,
      );
    }
  }
  const src = baseEnv ?? process.env;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(src)) if (v !== undefined) out[k] = v;
  out[SHADER_DIALECT_ENV] = value;
  return out;
}
