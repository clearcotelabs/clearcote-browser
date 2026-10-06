import { describe, it, expect } from "vitest";
import { withShaderDialect, defaultShaderDialect, SHADER_DIALECT_ENV } from "../src/shaderdialect.js";

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

describe("default for a Windows claim (2026-10-06)", () => {
  const WIN = ["--fingerprint=s", "--fingerprint-platform=windows"];
  const LINUX = ["--fingerprint=s", "--fingerprint-platform=linux"];

  it("a Windows claim on a Linux host gets hlsl", () => {
    // Measured on r30, GPU-less Linux: the Windows persona answered getTranslatedShaderSource with
    // SwiftShader text beside a Direct3D11 renderer string; with the dialect it answered in HLSL.
    expect(withShaderDialect(undefined, { PATH: "x" }, WIN, "linux")).toEqual({ PATH: "x", [SHADER_DIALECT_ENV]: "hlsl" });
  });

  it("nothing on a Windows host, for a Linux claim, or with no claim", () => {
    expect(defaultShaderDialect(WIN, "win32")).toBeUndefined();
    expect(defaultShaderDialect(LINUX, "linux")).toBeUndefined();
    expect(defaultShaderDialect([], "linux")).toBeUndefined();
    expect(withShaderDialect(undefined, undefined, LINUX, "linux")).toBeUndefined();
  });

  it("the last platform switch decides", () => {
    expect(defaultShaderDialect([...LINUX, "--fingerprint-platform=windows"], "linux")).toBe("hlsl");
    expect(defaultShaderDialect([...WIN, "--fingerprint-platform=linux"], "linux")).toBeUndefined();
  });

  it("false or an off spelling beats the default", () => {
    for (const off of [false, "off", "0", "none", ""] as const) {
      expect(withShaderDialect(off as never, { A: "1" }, WIN, "linux")).toEqual({ A: "1" });
    }
  });

  it("the caller's own exported variable is not overridden by the default", () => {
    expect(withShaderDialect(undefined, { [SHADER_DIALECT_ENV]: "mine" }, WIN, "linux")).toEqual({ [SHADER_DIALECT_ENV]: "mine" });
  });
});
