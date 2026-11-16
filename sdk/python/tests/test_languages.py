"""Chrome's language defaults (mirrors sdk/node/test/languages.test.ts and
sdk/dotnet/tests/Clearcote.Tests/LanguagesTests.cs)."""
import pytest

from clearcote._fingerprint import fingerprint_args
from clearcote._fonts import linux_locale_env
from clearcote._languages import (CHROME_UI_LOCALES, chrome_accept_languages, chrome_ui_locale,
                                  resolve_languages)
from clearcote.geoip import COUNTRY_LANG, accept_language_for_country

# Genuine Google Chrome 154 on Windows, fresh profile per OS locale (--lang=<os locale>):
# (os locale, navigator.languages, Intl locale). 2026-09-25, SESSION-CONTEXT/118.
GENUINE_CHROME_154 = [
    ("en-US", "en-US,en", "en-US"),
    ("en-GB", "en-GB,en-US,en", "en-GB"),
    ("en-CA", "en-GB,en-US,en", "en-GB"),
    ("fr-CA", "fr-FR,fr,en-US,en", "fr"),
    ("en-AU", "en-GB,en-US,en", "en-GB"),
    ("en-NZ", "en-GB,en-US,en", "en-GB"),
    ("en-IE", "en-GB,en-US,en", "en-GB"),
    ("en-IN", "en-GB,en-US,en", "en-GB"),
    ("hi-IN", "hi-IN,hi,en-US,en", "hi"),
    ("en-ZA", "en-GB,en-US,en", "en-GB"),
    ("en-SG", "en-GB,en-US,en", "en-GB"),
    ("de-DE", "de-DE,de,en-US,en", "de"),
    ("de-AT", "de-DE,de,en-US,en", "de"),
    ("de-CH", "de-DE,de,en-US,en", "de"),
    ("fr-CH", "fr-FR,fr,en-US,en", "fr"),
    ("it-CH", "it-IT,it,en-US,en", "it"),
    ("fr-FR", "fr-FR,fr,en-US,en", "fr"),
    ("nl-BE", "nl-NL,nl,en-US,en", "nl"),
    ("fr-BE", "fr-FR,fr,en-US,en", "fr"),
    ("nl-NL", "nl-NL,nl,en-US,en", "nl"),
    ("es-ES", "es-ES,es", "es"),
    ("es-MX", "es-419,es", "es-419"),
    ("es-AR", "es-419,es", "es-419"),
    ("es-CL", "es-419,es", "es-419"),
    ("es-CO", "es-419,es", "es-419"),
    ("es-US", "es-419,es", "es-419"),
    ("pt-PT", "pt-PT,pt,en-US,en", "pt-PT"),
    ("pt-BR", "pt-BR,pt,en-US,en", "pt-BR"),
    ("it-IT", "it-IT,it,en-US,en", "it"),
    ("pl-PL", "pl-PL,pl,en-US,en", "pl"),
    ("ru-RU", "ru-RU,ru,en-US,en", "ru"),
    ("uk-UA", "uk-UA,uk,en-US,en", "uk"),
    ("sv-SE", "sv-SE,sv,en-US,en", "sv"),
    ("nb-NO", "nb-NO,nb,no,nn,en-US,en", "nb"),
    ("da-DK", "da-DK,da,en-US,en", "da"),
    ("fi-FI", "fi-FI,fi,en-US,en", "fi"),
    ("cs-CZ", "cs-CZ,cs", "cs"),
    ("ro-RO", "ro-RO,ro,en-US,en", "ro"),
    ("hu-HU", "hu-HU,hu,en-US,en", "hu"),
    ("el-GR", "el-GR,el", "el"),
    ("tr-TR", "tr-TR,tr,en-US,en", "tr"),
    ("he-IL", "he-IL,he,en-US,en", "he"),
    ("ar-SA", "ar,en-US,en", "ar"),
    ("ar-AE", "ar,en-US,en", "ar"),
    ("ar-EG", "ar,en-US,en", "ar"),
    ("ja-JP", "ja,en-US,en", "ja"),
    ("ko-KR", "ko-KR,ko,en-US,en", "ko"),
    ("zh-CN", "zh-CN,zh", "zh-CN"),
    ("zh-HK", "zh-TW,zh,en-US,en", "zh-TW"),
    ("zh-TW", "zh-TW,zh,en-US,en", "zh-TW"),
    ("zh-SG", "zh-CN,zh", "zh-CN"),
    ("th-TH", "th-TH,th", "th"),
    ("vi-VN", "vi-VN,vi,fr-FR,fr,en-US,en", "vi"),
    ("id-ID", "id-ID,id,en-US,en", "id"),
    ("ms-MY", "en-US,en", "ms"),
    ("en-PH", "en-US,en", "en-US"),
    ("fil-PH", "fil,fil-PH,tl,en-US,en", "fil"),
    ("bg-BG", "bg-BG,bg", "bg"),
    ("hr-HR", "hr-HR,hr,en-US,en", "hr"),
    ("sk-SK", "sk-SK,sk,cs,en-US,en", "sk"),
    ("sl-SI", "sl-SI,sl,en-GB,en", "sl"),
    ("sr-RS", "sr-RS,sr,en-US,en", "sr"),
    ("lt-LT", "lt,en-US,en,ru,pl", "lt"),
    ("lv-LV", "lv-LV,lv,en-US,en", "lv"),
    ("et-EE", "et-EE,et,en-US,en", "et"),
    ("ca-ES", "ca-ES,ca", "ca"),
]


@pytest.mark.parametrize("os_locale,languages,ui_locale", GENUINE_CHROME_154)
def test_single_tag_matches_genuine_chrome(os_locale, languages, ui_locale):
    assert resolve_languages(os_locale) == (languages, ui_locale)
    args = fingerprint_args({"accept_language": os_locale, "platform": "windows"})
    assert "--accept-lang=" + languages in args
    assert "--lang=" + ui_locale in args


def test_every_ui_locale_has_a_default_list():
    for ui in CHROME_UI_LOCALES:
        assert chrome_ui_locale(ui) == ui
        langs = chrome_accept_languages(ui).split(",")
        assert langs and all(langs) and ";" not in ",".join(langs)
    # no translation of the default -> the source string (Malay, Afrikaans, Urdu, US English)
    for ui in ("ms", "af", "ur", "en-US"):
        assert chrome_accept_languages(ui) == "en-US,en"


def test_ui_locale_resolution_rules():
    assert chrome_ui_locale("en") == "en-US"
    assert chrome_ui_locale("en-JM") == "en-GB"
    assert chrome_ui_locale("es") == "es"
    assert chrome_ui_locale("es-PE") == "es-419"
    assert chrome_ui_locale("pt") == "pt-BR"
    assert chrome_ui_locale("pt-AO") == "pt-PT"
    assert chrome_ui_locale("zh") == "zh-CN"
    assert chrome_ui_locale("zh-MO") == "zh-TW"
    assert chrome_ui_locale("zh-Hant") == "zh-TW"
    assert chrome_ui_locale("zh-Hans-HK") == "zh-CN"
    assert chrome_ui_locale("de_AT") == "de"
    assert chrome_ui_locale("DE-at") == "de"
    assert chrome_ui_locale("iw-IL") == "he"
    assert chrome_ui_locale("no") == "nb"
    assert chrome_ui_locale("tl") == "fil"
    # languages Chrome ships no UI for (Icelandic, Basque, Afrikaans's neighbour Zulu, ...)
    for tag in ("is-IS", "eu-ES", "zu-ZA", "cy-GB", "", "  "):
        assert chrome_ui_locale(tag) is None


def test_list_is_kept_and_only_ui_locale_resolved():
    assert resolve_languages("de-AT,de,en") == ("de-AT,de,en", "de")
    assert resolve_languages("fr-CA,fr,en") == ("fr-CA,fr,en", "fr")
    assert resolve_languages("en-US,en") == ("en-US,en", "en-US")


def test_unknown_language_passes_through_unchanged():
    assert resolve_languages("is-IS") == ("is-IS", "is-IS")
    assert resolve_languages("is-IS,is,en") == ("is-IS,is,en", "is-IS")


def test_timezone_default_follows_the_callers_own_tag():
    tz = lambda al: [a for a in fingerprint_args({"accept_language": al}) if a.startswith("--timezone=")]
    assert tz("de-AT") == ["--timezone=Europe/Vienna"]  # not Berlin, though languages[0] is de-DE
    assert tz("en-CA") == ["--timezone=America/Toronto"]
    assert tz("ms-MY") == ["--timezone=Asia/Kuala_Lumpur"]
    assert tz("es-419") == ["--timezone=America/Mexico_City"]
    assert tz("de_at") == ["--timezone=Europe/Vienna"]
    assert tz("de-LU") == ["--timezone=Europe/Berlin"]  # language fallback


def test_passthrough_expands_the_same_way():
    args = fingerprint_args({"fingerprint": "off", "accept_language": "de-AT"})
    assert args == ["--fingerprint-passthrough", "--disable-gpu-fingerprint", "--disable-fingerprint-voices",
                    "--accept-lang=de-DE,de,en-US,en", "--lang=de"]


def test_geoip_countries_map_to_one_resolvable_os_locale():
    for cc, tag in COUNTRY_LANG.items():
        assert "," not in tag and ";" not in tag, cc
        assert chrome_ui_locale(tag), (cc, tag)
    assert accept_language_for_country("MY") == "ms-MY"
    assert accept_language_for_country("zz") == "en-US"


def test_linux_locale_env():
    assert linux_locale_env(["--lang=de"], platform="linux") == {"LANGUAGE": "de"}
    assert linux_locale_env(["--lang=en-GB"], platform="linux") == {"LANGUAGE": "en_GB"}
    assert linux_locale_env(["--lang=de", "--lang=fr"], platform="linux") == {"LANGUAGE": "fr"}
    assert linux_locale_env(["--lang=de"], platform="win32") == {}
    assert linux_locale_env(["--accept-lang=de"], platform="linux") == {}
    assert linux_locale_env([], platform="linux") == {}
