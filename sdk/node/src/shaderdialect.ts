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
 * OFF unless asked for. The re-translation is a different code path from the one that rendered, so
 * a shader the Vulkan backend accepts but the HLSL translator rejects falls back to the honest
 * SPIR-V for that shader. It is for callers who actually hit this check, not a default.
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

/**
 * Fold `CLEARCOTE_SHADER_DIALECT` into a launch env.
 *
 * Returns `baseEnv` untouched when no dialect is requested — including `undefined`, so the default
 * Playwright env is preserved rather than being replaced by a copy of `process.env`.
 *
 * Throws on an unknown dialect rather than ignoring it: a typo would otherwise look like it worked
 * while the engine kept reporting the honest dialect.
 */
export function withShaderDialect(
  dialect: string | undefined,
  baseEnv: Record<string, string | undefined> | undefined,
): Record<string, string | undefined> | undefined {
  if (!dialect) return baseEnv;
  const value = String(dialect).trim().toLowerCase();
  if (!VALID.includes(value)) {
    throw new Error(
      `shaderDialect must be one of ${VALID.join(", ")} (got ${JSON.stringify(dialect)})`,
    );
  }
  const src = baseEnv ?? process.env;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(src)) if (v !== undefined) out[k] = v;
  out[SHADER_DIALECT_ENV] = value;
  return out;
}
