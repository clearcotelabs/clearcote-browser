import { describe, it, expect } from "vitest";
import { withShaderDialect, SHADER_DIALECT_ENV } from "../src/shaderdialect.js";

describe("withShaderDialect", () => {
  it("is off by default and leaves the env untouched", () => {
    // An undefined env must stay undefined so Playwright uses its default child env, exactly as
    // before the option existed.
    expect(withShaderDialect(undefined, undefined)).toBeUndefined();
  });

  it("passes an existing env through unchanged when no dialect is asked for", () => {
    const base = { FONTCONFIG_FILE: "/tmp/fonts.conf" };
    expect(withShaderDialect(undefined, base)).toBe(base);
  });

  it("sets the variable for hlsl", () => {
    const env = withShaderDialect("hlsl", {})!;
    expect(env[SHADER_DIALECT_ENV]).toBe("hlsl");
  });

  it("normalises the value", () => {
    const env = withShaderDialect("  HLSL  ", {})!;
    expect(env[SHADER_DIALECT_ENV]).toBe("hlsl");
  });

  it("rejects an unknown dialect", () => {
    // Rejected rather than ignored: a typo would otherwise look like it worked while the engine
    // kept reporting the honest dialect.
    expect(() => withShaderDialect("glsl", {})).toThrow(/shaderDialect/);
  });

  it("keeps what the font wiring already put in the env", () => {
    const env = withShaderDialect("hlsl", { FONTCONFIG_FILE: "/tmp/fonts.conf" })!;
    expect(env.FONTCONFIG_FILE).toBe("/tmp/fonts.conf");
    expect(env[SHADER_DIALECT_ENV]).toBe("hlsl");
  });

  it("carries process.env when there is no base env", () => {
    // Playwright REPLACES the child env when env is set, so the parent environment has to come
    // along or the browser loses PATH.
    process.env.CC_TEST_MARKER = "1";
    try {
      const env = withShaderDialect("hlsl", undefined)!;
      expect(env.CC_TEST_MARKER).toBe("1");
    } finally {
      delete process.env.CC_TEST_MARKER;
    }
  });

  it("uses the variable name the engine reads", () => {
    // The engine reads this exact name from the GPU process environment; renaming either side
    // silently disables the feature.
    expect(SHADER_DIALECT_ENV).toBe("CLEARCOTE_SHADER_DIALECT");
  });
});
