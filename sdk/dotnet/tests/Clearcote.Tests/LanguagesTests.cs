using Xunit;

namespace Clearcote.Tests;

// Chrome's language defaults (mirrors sdk/python/tests/test_languages.py and
// sdk/node/test/languages.test.ts).
public class LanguagesTests
{
    // Genuine Google Chrome 154 on Windows, fresh profile per OS locale (--lang=<os locale>):
    // { os locale, navigator.languages, Intl locale }. 2026-09-25, SESSION-CONTEXT/118.
    public static readonly TheoryData<string, string, string> GenuineChrome154 = Rows(new[]
    {
        new object[] { "en-US", "en-US,en", "en-US" },
        new object[] { "en-GB", "en-GB,en-US,en", "en-GB" },
        new object[] { "en-CA", "en-GB,en-US,en", "en-GB" },
        new object[] { "fr-CA", "fr-FR,fr,en-US,en", "fr" },
        new object[] { "en-AU", "en-GB,en-US,en", "en-GB" },
        new object[] { "en-NZ", "en-GB,en-US,en", "en-GB" },
        new object[] { "en-IE", "en-GB,en-US,en", "en-GB" },
        new object[] { "en-IN", "en-GB,en-US,en", "en-GB" },
        new object[] { "hi-IN", "hi-IN,hi,en-US,en", "hi" },
        new object[] { "en-ZA", "en-GB,en-US,en", "en-GB" },
        new object[] { "en-SG", "en-GB,en-US,en", "en-GB" },
        new object[] { "de-DE", "de-DE,de,en-US,en", "de" },
        new object[] { "de-AT", "de-DE,de,en-US,en", "de" },
        new object[] { "de-CH", "de-DE,de,en-US,en", "de" },
        new object[] { "fr-CH", "fr-FR,fr,en-US,en", "fr" },
        new object[] { "it-CH", "it-IT,it,en-US,en", "it" },
        new object[] { "fr-FR", "fr-FR,fr,en-US,en", "fr" },
        new object[] { "nl-BE", "nl-NL,nl,en-US,en", "nl" },
        new object[] { "fr-BE", "fr-FR,fr,en-US,en", "fr" },
        new object[] { "nl-NL", "nl-NL,nl,en-US,en", "nl" },
        new object[] { "es-ES", "es-ES,es", "es" },
        new object[] { "es-MX", "es-419,es", "es-419" },
        new object[] { "es-AR", "es-419,es", "es-419" },
        new object[] { "es-CL", "es-419,es", "es-419" },
        new object[] { "es-CO", "es-419,es", "es-419" },
        new object[] { "es-US", "es-419,es", "es-419" },
        new object[] { "pt-PT", "pt-PT,pt,en-US,en", "pt-PT" },
        new object[] { "pt-BR", "pt-BR,pt,en-US,en", "pt-BR" },
        new object[] { "it-IT", "it-IT,it,en-US,en", "it" },
        new object[] { "pl-PL", "pl-PL,pl,en-US,en", "pl" },
        new object[] { "ru-RU", "ru-RU,ru,en-US,en", "ru" },
        new object[] { "uk-UA", "uk-UA,uk,en-US,en", "uk" },
        new object[] { "sv-SE", "sv-SE,sv,en-US,en", "sv" },
        new object[] { "nb-NO", "nb-NO,nb,no,nn,en-US,en", "nb" },
        new object[] { "da-DK", "da-DK,da,en-US,en", "da" },
        new object[] { "fi-FI", "fi-FI,fi,en-US,en", "fi" },
        new object[] { "cs-CZ", "cs-CZ,cs", "cs" },
        new object[] { "ro-RO", "ro-RO,ro,en-US,en", "ro" },
        new object[] { "hu-HU", "hu-HU,hu,en-US,en", "hu" },
        new object[] { "el-GR", "el-GR,el", "el" },
        new object[] { "tr-TR", "tr-TR,tr,en-US,en", "tr" },
        new object[] { "he-IL", "he-IL,he,en-US,en", "he" },
        new object[] { "ar-SA", "ar,en-US,en", "ar" },
        new object[] { "ar-AE", "ar,en-US,en", "ar" },
        new object[] { "ar-EG", "ar,en-US,en", "ar" },
        new object[] { "ja-JP", "ja,en-US,en", "ja" },
        new object[] { "ko-KR", "ko-KR,ko,en-US,en", "ko" },
        new object[] { "zh-CN", "zh-CN,zh", "zh-CN" },
        new object[] { "zh-HK", "zh-TW,zh,en-US,en", "zh-TW" },
        new object[] { "zh-TW", "zh-TW,zh,en-US,en", "zh-TW" },
        new object[] { "zh-SG", "zh-CN,zh", "zh-CN" },
        new object[] { "th-TH", "th-TH,th", "th" },
        new object[] { "vi-VN", "vi-VN,vi,fr-FR,fr,en-US,en", "vi" },
        new object[] { "id-ID", "id-ID,id,en-US,en", "id" },
        new object[] { "ms-MY", "en-US,en", "ms" },
        new object[] { "en-PH", "en-US,en", "en-US" },
        new object[] { "fil-PH", "fil,fil-PH,tl,en-US,en", "fil" },
        new object[] { "bg-BG", "bg-BG,bg", "bg" },
        new object[] { "hr-HR", "hr-HR,hr,en-US,en", "hr" },
        new object[] { "sk-SK", "sk-SK,sk,cs,en-US,en", "sk" },
        new object[] { "sl-SI", "sl-SI,sl,en-GB,en", "sl" },
        new object[] { "sr-RS", "sr-RS,sr,en-US,en", "sr" },
        new object[] { "lt-LT", "lt,en-US,en,ru,pl", "lt" },
        new object[] { "lv-LV", "lv-LV,lv,en-US,en", "lv" },
        new object[] { "et-EE", "et-EE,et,en-US,en", "et" },
        new object[] { "ca-ES", "ca-ES,ca", "ca" },
    });

    private static TheoryData<string, string, string> Rows(object[][] rows)
    {
        var data = new TheoryData<string, string, string>();
        foreach (var r in rows) data.Add((string)r[0], (string)r[1], (string)r[2]);
        return data;
    }

    [Theory]
    [MemberData(nameof(GenuineChrome154))]
    public void Single_tag_matches_genuine_chrome(string osLocale, string languages, string uiLocale)
    {
        Assert.Equal((languages, (string?)uiLocale), Languages.ResolveLanguages(osLocale));
        var args = Fingerprint.Args(new FingerprintOptions { AcceptLanguage = osLocale, Platform = "windows" });
        Assert.Contains($"--accept-lang={languages}", args);
        Assert.Contains($"--lang={uiLocale}", args);
    }

    [Fact]
    public void Every_ui_locale_has_a_default_list()
    {
        foreach (var ui in Languages.ChromeUiLocales)
        {
            Assert.Equal(ui, Languages.ChromeUiLocale(ui));
            var langs = Languages.ChromeAcceptLanguages(ui).Split(',');
            Assert.NotEmpty(langs);
            Assert.All(langs, l => Assert.NotEmpty(l));
            Assert.DoesNotContain(";", string.Join(",", langs));
        }
        // no translation of the default -> the source string (Malay, Afrikaans, Urdu, US English)
        foreach (var ui in new[] { "ms", "af", "ur", "en-US" }) Assert.Equal("en-US,en", Languages.ChromeAcceptLanguages(ui));
    }

    [Theory]
    [InlineData("en", "en-US")]
    [InlineData("en-JM", "en-GB")]
    [InlineData("es", "es")]
    [InlineData("es-PE", "es-419")]
    [InlineData("pt", "pt-BR")]
    [InlineData("pt-AO", "pt-PT")]
    [InlineData("zh", "zh-CN")]
    [InlineData("zh-MO", "zh-TW")]
    [InlineData("zh-Hant", "zh-TW")]
    [InlineData("zh-Hans-HK", "zh-CN")]
    [InlineData("de_AT", "de")]
    [InlineData("DE-at", "de")]
    [InlineData("iw-IL", "he")]
    [InlineData("no", "nb")]
    [InlineData("tl", "fil")]
    [InlineData("is-IS", null)]
    [InlineData("eu-ES", null)]
    [InlineData("zu-ZA", null)]
    [InlineData("cy-GB", null)]
    [InlineData("", null)]
    [InlineData("  ", null)]
    public void Ui_locale_resolution_rules(string tag, string? ui) => Assert.Equal(ui, Languages.ChromeUiLocale(tag));

    [Fact]
    public void List_is_kept_and_only_ui_locale_resolved()
    {
        Assert.Equal(("de-AT,de,en", (string?)"de"), Languages.ResolveLanguages("de-AT,de,en"));
        Assert.Equal(("fr-CA,fr,en", (string?)"fr"), Languages.ResolveLanguages("fr-CA,fr,en"));
        Assert.Equal(("en-US,en", (string?)"en-US"), Languages.ResolveLanguages("en-US,en"));
    }

    [Fact]
    public void Unknown_language_passes_through_unchanged()
    {
        Assert.Equal(("is-IS", (string?)"is-IS"), Languages.ResolveLanguages("is-IS"));
        Assert.Equal(("is-IS,is,en", (string?)"is-IS"), Languages.ResolveLanguages("is-IS,is,en"));
    }

    [Theory]
    [InlineData("de-AT", "Europe/Vienna")]  // not Berlin, though languages[0] is de-DE
    [InlineData("en-CA", "America/Toronto")]
    [InlineData("ms-MY", "Asia/Kuala_Lumpur")]
    [InlineData("es-419", "America/Mexico_City")]
    [InlineData("de_at", "Europe/Vienna")]
    [InlineData("de-LU", "Europe/Berlin")]  // language fallback
    public void Timezone_default_follows_the_callers_own_tag(string lang, string tz)
        => Assert.Contains($"--timezone={tz}", Fingerprint.Args(new FingerprintOptions { AcceptLanguage = lang }));

    [Fact]
    public void Passthrough_expands_the_same_way()
        => Assert.Equal(new[] { "--fingerprint-passthrough", "--disable-gpu-fingerprint", "--disable-fingerprint-voices", "--accept-lang=de-DE,de,en-US,en", "--lang=de" },
            Fingerprint.Args(new FingerprintOptions { Fingerprint = "off", AcceptLanguage = "de-AT" }));

    [Fact]
    public void Geoip_countries_map_to_one_resolvable_os_locale()
    {
        foreach (var (cc, tag) in GeoIp.CountryLang)
        {
            Assert.DoesNotContain(",", tag);
            Assert.DoesNotContain(";", tag);
            Assert.True(Languages.ChromeUiLocale(tag) is not null, $"{cc} {tag}");
        }
        Assert.Equal("ms-MY", GeoIp.AcceptLanguageForCountry("MY"));
        Assert.Equal("en-US", GeoIp.AcceptLanguageForCountry("zz"));
    }

    [Fact]
    public void Linux_language_env()
    {
        Assert.Equal("de", Languages.LinuxLanguageEnv(new[] { "--lang=de" }, isLinux: true));
        Assert.Equal("en_GB", Languages.LinuxLanguageEnv(new[] { "--lang=en-GB" }, isLinux: true));
        Assert.Equal("fr", Languages.LinuxLanguageEnv(new[] { "--lang=de", "--lang=fr" }, isLinux: true));
        Assert.Null(Languages.LinuxLanguageEnv(new[] { "--lang=de" }, isLinux: false));
        Assert.Null(Languages.LinuxLanguageEnv(new[] { "--accept-lang=de" }, isLinux: true));
        Assert.Null(Languages.LinuxLanguageEnv(Array.Empty<string>(), isLinux: true));
    }

    [Fact]
    public void Linux_language_env_merge_keeps_the_callers_value()
    {
        using var _ = new Sandbox().Env("LANGUAGE", "en_US:en");  // a host desktop's own value loses
        var env = Languages.ApplyLinuxLanguage(new[] { "--lang=de" }, null, isLinux: true);
        Assert.Equal("de", env!["LANGUAGE"]);
        Assert.True(env.ContainsKey("PATH") || env.ContainsKey("Path"));  // parent env carried along
        var caller = new Dictionary<string, string> { ["LANGUAGE"] = "fr", ["X"] = "1" };
        Assert.Same(caller, Languages.ApplyLinuxLanguage(new[] { "--lang=de" }, caller, isLinux: true));
        var other = new Dictionary<string, string> { ["X"] = "1" };
        var merged = Languages.ApplyLinuxLanguage(new[] { "--lang=de" }, other, isLinux: true)!;
        Assert.Equal("de", merged["LANGUAGE"]);
        Assert.Equal("1", merged["X"]);
        Assert.Null(Languages.ApplyLinuxLanguage(new[] { "--lang=de" }, null, isLinux: false));
    }
}
