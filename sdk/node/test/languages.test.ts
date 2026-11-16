import { describe, it, expect } from "vitest";
import { fingerprintArgs } from "../src/fingerprint.js";
import { linuxLocaleEnv, fontLaunchEnv } from "../src/fonts.js";
import { CHROME_UI_LOCALES, chromeAcceptLanguages, chromeUiLocale, resolveLanguages } from "../src/languages.js";
import { COUNTRY_LANG, acceptLanguageForCountry } from "../src/geoip.js";

// Chrome's language defaults (mirrors sdk/python/tests/test_languages.py and
// sdk/dotnet/tests/Clearcote.Tests/LanguagesTests.cs).
// Genuine Google Chrome 154 on Windows, fresh profile per OS locale (--lang=<os locale>):
// [os locale, navigator.languages, Intl locale]. 2026-09-25, SESSION-CONTEXT/118.
const GENUINE_CHROME_154: [string, string, string][] = [
  ["en-US", "en-US,en", "en-US"],
  ["en-GB", "en-GB,en-US,en", "en-GB"],
  ["en-CA", "en-GB,en-US,en", "en-GB"],
  ["fr-CA", "fr-FR,fr,en-US,en", "fr"],
  ["en-AU", "en-GB,en-US,en", "en-GB"],
  ["en-NZ", "en-GB,en-US,en", "en-GB"],
  ["en-IE", "en-GB,en-US,en", "en-GB"],
  ["en-IN", "en-GB,en-US,en", "en-GB"],
  ["hi-IN", "hi-IN,hi,en-US,en", "hi"],
  ["en-ZA", "en-GB,en-US,en", "en-GB"],
  ["en-SG", "en-GB,en-US,en", "en-GB"],
  ["de-DE", "de-DE,de,en-US,en", "de"],
  ["de-AT", "de-DE,de,en-US,en", "de"],
  ["de-CH", "de-DE,de,en-US,en", "de"],
  ["fr-CH", "fr-FR,fr,en-US,en", "fr"],
  ["it-CH", "it-IT,it,en-US,en", "it"],
  ["fr-FR", "fr-FR,fr,en-US,en", "fr"],
  ["nl-BE", "nl-NL,nl,en-US,en", "nl"],
  ["fr-BE", "fr-FR,fr,en-US,en", "fr"],
  ["nl-NL", "nl-NL,nl,en-US,en", "nl"],
  ["es-ES", "es-ES,es", "es"],
  ["es-MX", "es-419,es", "es-419"],
  ["es-AR", "es-419,es", "es-419"],
  ["es-CL", "es-419,es", "es-419"],
  ["es-CO", "es-419,es", "es-419"],
  ["es-US", "es-419,es", "es-419"],
  ["pt-PT", "pt-PT,pt,en-US,en", "pt-PT"],
  ["pt-BR", "pt-BR,pt,en-US,en", "pt-BR"],
  ["it-IT", "it-IT,it,en-US,en", "it"],
  ["pl-PL", "pl-PL,pl,en-US,en", "pl"],
  ["ru-RU", "ru-RU,ru,en-US,en", "ru"],
  ["uk-UA", "uk-UA,uk,en-US,en", "uk"],
  ["sv-SE", "sv-SE,sv,en-US,en", "sv"],
  ["nb-NO", "nb-NO,nb,no,nn,en-US,en", "nb"],
  ["da-DK", "da-DK,da,en-US,en", "da"],
  ["fi-FI", "fi-FI,fi,en-US,en", "fi"],
  ["cs-CZ", "cs-CZ,cs", "cs"],
  ["ro-RO", "ro-RO,ro,en-US,en", "ro"],
  ["hu-HU", "hu-HU,hu,en-US,en", "hu"],
  ["el-GR", "el-GR,el", "el"],
  ["tr-TR", "tr-TR,tr,en-US,en", "tr"],
  ["he-IL", "he-IL,he,en-US,en", "he"],
  ["ar-SA", "ar,en-US,en", "ar"],
  ["ar-AE", "ar,en-US,en", "ar"],
  ["ar-EG", "ar,en-US,en", "ar"],
  ["ja-JP", "ja,en-US,en", "ja"],
  ["ko-KR", "ko-KR,ko,en-US,en", "ko"],
  ["zh-CN", "zh-CN,zh", "zh-CN"],
  ["zh-HK", "zh-TW,zh,en-US,en", "zh-TW"],
  ["zh-TW", "zh-TW,zh,en-US,en", "zh-TW"],
  ["zh-SG", "zh-CN,zh", "zh-CN"],
  ["th-TH", "th-TH,th", "th"],
  ["vi-VN", "vi-VN,vi,fr-FR,fr,en-US,en", "vi"],
  ["id-ID", "id-ID,id,en-US,en", "id"],
  ["ms-MY", "en-US,en", "ms"],
  ["en-PH", "en-US,en", "en-US"],
  ["fil-PH", "fil,fil-PH,tl,en-US,en", "fil"],
  ["bg-BG", "bg-BG,bg", "bg"],
  ["hr-HR", "hr-HR,hr,en-US,en", "hr"],
  ["sk-SK", "sk-SK,sk,cs,en-US,en", "sk"],
  ["sl-SI", "sl-SI,sl,en-GB,en", "sl"],
  ["sr-RS", "sr-RS,sr,en-US,en", "sr"],
  ["lt-LT", "lt,en-US,en,ru,pl", "lt"],
  ["lv-LV", "lv-LV,lv,en-US,en", "lv"],
  ["et-EE", "et-EE,et,en-US,en", "et"],
  ["ca-ES", "ca-ES,ca", "ca"],
];

describe("resolveLanguages", () => {
  it.each(GENUINE_CHROME_154)("%s matches genuine Chrome", (osLocale, languages, uiLocale) => {
    expect(resolveLanguages(osLocale)).toEqual([languages, uiLocale]);
    const args = fingerprintArgs({ acceptLanguage: osLocale, platform: "windows" });
    expect(args).toContain(`--accept-lang=${languages}`);
    expect(args).toContain(`--lang=${uiLocale}`);
  });

  it("has a default list for every UI locale", () => {
    for (const ui of CHROME_UI_LOCALES) {
      expect(chromeUiLocale(ui)).toBe(ui);
      const langs = chromeAcceptLanguages(ui).split(",");
      expect(langs.length).toBeGreaterThan(0);
      expect(langs.every((l) => l.length > 0)).toBe(true);
      expect(langs.join(",")).not.toContain(";");
    }
    // no translation of the default -> the source string (Malay, Afrikaans, Urdu, US English)
    for (const ui of ["ms", "af", "ur", "en-US"]) expect(chromeAcceptLanguages(ui)).toBe("en-US,en");
  });

  it("resolves UI locales the way Chrome does", () => {
    expect(chromeUiLocale("en")).toBe("en-US");
    expect(chromeUiLocale("en-JM")).toBe("en-GB");
    expect(chromeUiLocale("es")).toBe("es");
    expect(chromeUiLocale("es-PE")).toBe("es-419");
    expect(chromeUiLocale("pt")).toBe("pt-BR");
    expect(chromeUiLocale("pt-AO")).toBe("pt-PT");
    expect(chromeUiLocale("zh")).toBe("zh-CN");
    expect(chromeUiLocale("zh-MO")).toBe("zh-TW");
    expect(chromeUiLocale("zh-Hant")).toBe("zh-TW");
    expect(chromeUiLocale("zh-Hans-HK")).toBe("zh-CN");
    expect(chromeUiLocale("de_AT")).toBe("de");
    expect(chromeUiLocale("DE-at")).toBe("de");
    expect(chromeUiLocale("iw-IL")).toBe("he");
    expect(chromeUiLocale("no")).toBe("nb");
    expect(chromeUiLocale("tl")).toBe("fil");
    for (const tag of ["is-IS", "eu-ES", "zu-ZA", "cy-GB", "", "  "]) expect(chromeUiLocale(tag)).toBeUndefined();
  });

  it("keeps a list verbatim and only resolves the UI locale", () => {
    expect(resolveLanguages("de-AT,de,en")).toEqual(["de-AT,de,en", "de"]);
    expect(resolveLanguages("fr-CA,fr,en")).toEqual(["fr-CA,fr,en", "fr"]);
    expect(resolveLanguages("en-US,en")).toEqual(["en-US,en", "en-US"]);
  });

  it("passes an unknown language through unchanged", () => {
    expect(resolveLanguages("is-IS")).toEqual(["is-IS", "is-IS"]);
    expect(resolveLanguages("is-IS,is,en")).toEqual(["is-IS,is,en", "is-IS"]);
  });

  it("keys the timezone default off the caller's own tag", () => {
    const tz = (al: string) => fingerprintArgs({ acceptLanguage: al }).filter((a) => a.startsWith("--timezone="));
    expect(tz("de-AT")).toEqual(["--timezone=Europe/Vienna"]); // not Berlin, though languages[0] is de-DE
    expect(tz("en-CA")).toEqual(["--timezone=America/Toronto"]);
    expect(tz("ms-MY")).toEqual(["--timezone=Asia/Kuala_Lumpur"]);
    expect(tz("es-419")).toEqual(["--timezone=America/Mexico_City"]);
    expect(tz("de_at")).toEqual(["--timezone=Europe/Vienna"]);
    expect(tz("de-LU")).toEqual(["--timezone=Europe/Berlin"]); // language fallback
  });

  it("expands the same way in pass-through mode", () => {
    expect(fingerprintArgs({ fingerprint: "off", acceptLanguage: "de-AT" })).toEqual([
      "--fingerprint-passthrough", "--disable-gpu-fingerprint", "--disable-fingerprint-voices", "--accept-lang=de-DE,de,en-US,en", "--lang=de",
    ]);
  });

  it("maps every geoip country to one resolvable OS locale", () => {
    for (const [cc, tag] of Object.entries(COUNTRY_LANG)) {
      expect(tag, cc).not.toMatch(/[,;]/);
      expect(chromeUiLocale(tag), `${cc} ${tag}`).toBeDefined();
    }
    expect(acceptLanguageForCountry("MY")).toBe("ms-MY");
    expect(acceptLanguageForCountry("zz")).toBe("en-US");
  });
});

describe("linuxLocaleEnv", () => {
  it("sets LANGUAGE from the last --lang on Linux only", () => {
    expect(linuxLocaleEnv(["--lang=de"], "linux")).toEqual({ LANGUAGE: "de" });
    expect(linuxLocaleEnv(["--lang=en-GB"], "linux")).toEqual({ LANGUAGE: "en_GB" });
    expect(linuxLocaleEnv(["--lang=de", "--lang=fr"], "linux")).toEqual({ LANGUAGE: "fr" });
    expect(linuxLocaleEnv(["--lang=de"], "win32")).toEqual({});
    expect(linuxLocaleEnv(["--accept-lang=de"], "linux")).toEqual({});
    expect(linuxLocaleEnv([], "linux")).toEqual({});
  });

  it.runIf(process.platform === "linux")("overrides the host LANGUAGE, but the caller's env wins", () => {
    const saved = process.env.LANGUAGE;
    process.env.LANGUAGE = "en_US:en";
    try {
      expect(fontLaunchEnv("/nonexistent/chrome", undefined, ["--lang=de"])?.LANGUAGE).toBe("de");
      expect(fontLaunchEnv("/nonexistent/chrome", { LANGUAGE: "fr" }, ["--lang=de"])?.LANGUAGE).toBe("fr");
    } finally {
      if (saved === undefined) delete process.env.LANGUAGE;
      else process.env.LANGUAGE = saved;
    }
  });

  it.runIf(process.platform !== "linux")("adds nothing off Linux", () => {
    expect(fontLaunchEnv("/nonexistent/chrome", undefined, ["--lang=de"])).toBeUndefined();
  });
});
