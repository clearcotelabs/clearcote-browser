import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { linuxFontEnv, fontLaunchEnv } from "../src/fonts.js";

const withPlatform = (plat: string, fn: () => void) => {
  const orig = Object.getOwnPropertyDescriptor(process, "platform");
  Object.defineProperty(process, "platform", { value: plat, configurable: true });
  try {
    fn();
  } finally {
    if (orig) Object.defineProperty(process, "platform", orig);
  }
};

function makeBundle() {
  const dir = mkdtempSync(join(tmpdir(), "ccfonts-"));
  const fonts = join(dir, "fonts");
  mkdirSync(fonts);
  writeFileSync(
    join(fonts, "fonts.conf.template"),
    "<fontconfig><dir>@FONTS_DIR@</dir><cachedir>@CACHE_DIR@</cachedir></fontconfig>",
  );
  return { exe: join(dir, "chrome"), fonts };
}

describe("linuxFontEnv", () => {
  it("returns {} on non-linux (fonts are a Linux-only concern)", () => {
    withPlatform("win32", () => {
      expect(linuxFontEnv(makeBundle().exe)).toEqual({});
    });
  });

  it("on linux, materializes a conf with substituted paths + returns FONTCONFIG_FILE", () => {
    withPlatform("linux", () => {
      const { exe, fonts } = makeBundle();
      const env = linuxFontEnv(exe);
      expect(env.FONTCONFIG_FILE).toBe(join(fonts, "fonts.generated.conf"));
      const conf = readFileSync(env.FONTCONFIG_FILE, "utf8");
      expect(conf).not.toContain("@FONTS_DIR@"); // placeholder substituted
      expect(conf).not.toContain("@CACHE_DIR@");
      expect(conf).toContain(fonts); // real fonts dir wired in
    });
  });

  it("returns {} on linux when the binary ships no fonts/ bundle", () => {
    withPlatform("linux", () => {
      const dir = mkdtempSync(join(tmpdir(), "ccnofont-"));
      expect(linuxFontEnv(join(dir, "chrome"))).toEqual({});
    });
  });
});

describe("fontLaunchEnv", () => {
  it("returns undefined when there's nothing to add (non-linux, no caller env)", () => {
    withPlatform("win32", () => {
      expect(fontLaunchEnv(makeBundle().exe)).toBeUndefined();
    });
  });

  it("merges process.env + bundled FONTCONFIG_FILE on linux; caller env wins", () => {
    withPlatform("linux", () => {
      const { exe } = makeBundle();
      const env = fontLaunchEnv(exe, { MYVAR: "1" });
      expect(env).toBeDefined();
      expect(String(env!.FONTCONFIG_FILE)).toContain("fonts.generated.conf");
      expect(env!.MYVAR).toBe("1"); // caller-supplied
      expect(env!.PATH).toBeDefined(); // process.env included (Playwright replaces env when set)
    });
  });

  it("still passes a caller env through on non-linux (no font bundle)", () => {
    withPlatform("win32", () => {
      const env = fontLaunchEnv(makeBundle().exe, { MYVAR: "x" });
      expect(env?.MYVAR).toBe("x");
      expect("FONTCONFIG_FILE" in (env ?? {})).toBe(false);
    });
  });
});
