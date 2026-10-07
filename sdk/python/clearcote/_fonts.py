"""Linux font wiring.

The Linux release bundles metric-compatible font clones (Segoe UI->Selawik, Arial->Arimo,
Times New Roman->Tinos, ...) under ``<bin_dir>/fonts/`` with a self-contained
``fonts.conf.template``. On a bare server/container the Windows families (and even the
standard fontconfig metric-alias rules) are absent, so a page asking for "Segoe UI"
collapses to a single default -- a detectable render + an absent-font tell.

At launch we materialize the template (substituting the real fonts dir + a writable cache
dir) and point FONTCONFIG_FILE at it, so the clones resolve without depending on the host's
/etc/fonts. No-op on non-Linux and on older binaries that ship no ``fonts/``.

Because the template is self-contained, fonts installed on the host are invisible to the
browser. Two settings add directories to it:

* ``font_dirs`` / ``CLEARCOTE_FONT_DIRS`` -- your own fonts, typically a copy of a Windows
  machine's ``C:\\Windows\\Fonts``. Listed ahead of the bundle. On an engine that lets a genuine
  face win over its substitute (it carries GENUINE_FACES_SWITCH; PRO r32+), every family they
  provide is also used as itself: its lookalike rule (Arial->Arimo, Segoe UI->Selawik, ...) is
  dropped, and the names and CSS generics that resolve to it on Windows (Helvetica->Arial,
  sans-serif->Arial, system-ui->Segoe UI, ...) point at it. An older engine still draws every family
  a persona lists through its own substitute, so the rules stay there: changing them would split
  Helvetica from Arial. The directories still serve the characters the bundle cannot draw.
* ``CLEARCOTE_FALLBACK_FONT_DIRS`` -- fonts used only for characters nothing else covers
  (CJK, Indic, emoji, ...). Listed after the bundle and change no rules. The Docker image sets
  it to the script fonts it installs.

Both are lists separated by ``os.pathsep``. Never point either at all of ``/usr/share/fonts``:
the host's Latin families then compete with the clones and change the Windows widths.
"""

import hashlib
import os
import re
import struct
import sys
import tempfile

FONT_DIRS_ENV = "CLEARCOTE_FONT_DIRS"
FALLBACK_FONT_DIRS_ENV = "CLEARCOTE_FALLBACK_FONT_DIRS"

_FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")

#: Carried by an engine that lets a genuine Windows face installed here win over its substitute (PRO
#: r32+). The SDK never passes it: its presence in the binary is the marker. Measured on an engine
#: without it: with a genuine Arial in CLEARCOTE_FONT_DIRS and the rules changed, "Helvetica" reached
#: the genuine file while "Arial" stayed on the engine's Arimo -- two widths no Windows machine has.
GENUINE_FACES_SWITCH = "disable-genuine-font-faces"

#: The Windows family each non-Windows name in the template stands for (the Windows registry's
#: FontSubstitutes): when the genuine family is present, the rule points at it instead of a clone.
_WINDOWS_ALIASES = {"helvetica": "Arial", "times": "Times New Roman", "courier": "Courier New"}

#: What each CSS generic resolves to in Chrome on Windows (measured: see fonts.conf.template).
_GENERIC_WINDOWS = {"sans-serif": "Arial", "serif": "Times New Roman", "monospace": "Consolas",
                    "system-ui": "Segoe UI"}

#: The Windows families the bundle stands in for with a lookalike; `clearcote info` reports which
#: of them your own fonts provide for real.
KEY_WINDOWS_FAMILIES = ("Arial", "Segoe UI", "Calibri", "Cambria", "Consolas", "Courier New", "Georgia",
                        "Times New Roman", "Verdana", "Tahoma", "Trebuchet MS", "Comic Sans MS", "Impact")

#: One character per script that Windows 10/11 renders out of the box. A script with no font for its
#: character draws empty boxes, which a page tells apart from real glyphs with one fillText.
SCRIPT_SAMPLES = (
    ("Latin", 0x0041), ("Greek", 0x03A9), ("Cyrillic", 0x0416), ("Hebrew", 0x05D0), ("Arabic", 0x0628),
    ("Armenian", 0x0531), ("Georgian", 0x10D0), ("Devanagari", 0x0915), ("Bengali", 0x0995),
    ("Gurmukhi", 0x0A15), ("Gujarati", 0x0A95), ("Oriya", 0x0B15), ("Tamil", 0x0B95), ("Telugu", 0x0C15),
    ("Kannada", 0x0C95), ("Malayalam", 0x0D15), ("Sinhala", 0x0D9A), ("Thai", 0x0E01), ("Lao", 0x0E81),
    ("Tibetan", 0x0F40), ("Myanmar", 0x1000), ("Ethiopic", 0x1200), ("Cherokee", 0x13A0),
    ("Khmer", 0x1780), ("Mongolian", 0x1820), ("Hangul", 0xAC00), ("Kana", 0x3042),
    ("CJK ideographs", 0x4E2D), ("Emoji", 0x1F600),
)
#: Emoji count only from a colour font: a monochrome glyph (DejaVu has one) is not what Windows draws.
_COLOR_TABLES = (b"COLR", b"CBDT", b"sbix", b"SVG ")


# --- reading font files (just the name and cmap tables; no fontconfig needed) -------------------

def _faces(fh):
    """Offsets of the sfnt faces in an open font file: one for .ttf/.otf, several for a collection."""
    head = fh.read(12)
    if len(head) < 12:
        return []
    if head[:4] == b"ttcf":
        (count,) = struct.unpack(">I", head[8:12])
        data = fh.read(4 * min(count, 1024))
        return [o for (o,) in struct.iter_unpack(">I", data[:len(data) - len(data) % 4])]
    return [0]


def _tables(fh, offset):
    fh.seek(offset)
    head = fh.read(12)
    if len(head) < 12:
        return {}
    (num,) = struct.unpack(">H", head[4:6])
    data = fh.read(16 * num)
    tables = {}
    for i in range(len(data) // 16):
        tag, _sum, off, length = struct.unpack(">4sIII", data[16 * i:16 * i + 16])
        tables[tag] = (off, length)
    return tables


def _families(fh, table):
    """Family names (name IDs 1 and 16, every language) of one face, lower-cased."""
    off, length = table
    fh.seek(off)
    data = fh.read(length)
    if len(data) < 6:
        return set()
    _fmt, count, strings = struct.unpack(">HHH", data[:6])
    names = set()
    for i in range(count):
        rec = data[6 + 12 * i:18 + 12 * i]
        if len(rec) < 12:
            break
        platform, encoding, _lang, name_id, n, o = struct.unpack(">6H", rec)
        if name_id not in (1, 16):
            continue
        raw = data[strings + o:strings + o + n]
        try:
            if platform in (0, 3):
                text = raw.decode("utf-16-be")
            elif platform == 1 and encoding == 0:
                text = raw.decode("mac_roman")
            else:
                continue
        except UnicodeDecodeError:
            continue
        if text.strip():
            names.add(text.strip().lower())
    return names


def _cmap_covers(fh, table, codepoints):
    """Which of ``codepoints`` the face maps to a real glyph (format 4 and 12 subtables)."""
    off, length = table
    fh.seek(off)
    data = fh.read(length)
    if len(data) < 4:
        return set()
    (count,) = struct.unpack(">H", data[2:4])
    subtables = {}
    for i in range(count):
        rec = data[4 + 8 * i:12 + 8 * i]
        if len(rec) < 8:
            break
        platform, encoding, sub = struct.unpack(">HHI", rec)
        if sub + 2 <= len(data):
            (fmt,) = struct.unpack(">H", data[sub:sub + 2])
            subtables.setdefault((platform, encoding, fmt), sub)
    for key in ((3, 10, 12), (0, 6, 12), (0, 4, 12), (3, 1, 4), (0, 3, 4), (0, 2, 4), (0, 1, 4), (0, 0, 4)):
        if key in subtables:
            sub = subtables[key]
            return (_fmt12(data, sub, codepoints) if key[2] == 12 else _fmt4(data, sub, codepoints))
    return set()


def _fmt12(data, sub, codepoints):
    if sub + 16 > len(data):
        return set()
    (groups,) = struct.unpack(">I", data[sub + 12:sub + 16])
    ranges = []
    for i in range(min(groups, (len(data) - sub - 16) // 12)):
        start, end, glyph = struct.unpack(">III", data[sub + 16 + 12 * i:sub + 28 + 12 * i])
        ranges.append((start, end, glyph))
    return {c for c in codepoints for s, e, g in ranges if s <= c <= e and g + (c - s) != 0}


def _fmt4(data, sub, codepoints):
    if sub + 14 > len(data):
        return set()
    (seg2,) = struct.unpack(">H", data[sub + 6:sub + 8])
    ends_at = sub + 14
    starts_at = ends_at + seg2 + 2
    deltas_at = starts_at + seg2
    ranges_at = deltas_at + seg2
    if ranges_at + seg2 > len(data):
        return set()
    covered = set()
    for c in codepoints:
        if c > 0xFFFF:
            continue
        for i in range(seg2 // 2):
            (end,) = struct.unpack(">H", data[ends_at + 2 * i:ends_at + 2 * i + 2])
            if c > end:
                continue
            (start,) = struct.unpack(">H", data[starts_at + 2 * i:starts_at + 2 * i + 2])
            if c < start:
                break
            (delta,) = struct.unpack(">h", data[deltas_at + 2 * i:deltas_at + 2 * i + 2])
            (range_off,) = struct.unpack(">H", data[ranges_at + 2 * i:ranges_at + 2 * i + 2])
            if range_off == 0:
                glyph = (c + delta) & 0xFFFF
            else:
                at = ranges_at + 2 * i + range_off + 2 * (c - start)
                if at + 2 > len(data):
                    break
                (glyph,) = struct.unpack(">H", data[at:at + 2])
                if glyph:
                    glyph = (glyph + delta) & 0xFFFF
            if glyph:
                covered.add(c)
            break
    return covered


def read_font(path, codepoints=()):
    """``(families, covered, color)`` of a font file: its family names (lower-cased, every face of a
    collection), which of ``codepoints`` it covers, and whether a face with a colour table covers any
    of them. ``(set(), set(), False)`` for a file that is not a readable font."""
    families, covered, color = set(), set(), False
    try:
        with open(path, "rb") as fh:
            for face in _faces(fh):
                tables = _tables(fh, face)
                if b"name" in tables:
                    families |= _families(fh, tables[b"name"])
                if codepoints and b"cmap" in tables:
                    got = _cmap_covers(fh, tables[b"cmap"], codepoints)
                    covered |= got
                    if got and any(t in tables for t in _COLOR_TABLES):
                        color = True
    except (OSError, struct.error, ValueError):
        return set(), set(), False
    return families, covered, color


def font_files(dirs):
    """Every font file under ``dirs`` (recursively, as fontconfig reads a ``<dir>``), sorted."""
    found = []
    for d in dirs:
        for root, _subdirs, files in os.walk(d):
            found.extend(os.path.join(root, f) for f in files if f.lower().endswith(_FONT_EXTS))
    return sorted(found)


_family_cache = {}


def dir_families(dirs):
    """Lower-cased family names of every font under ``dirs`` (cached per file size and mtime)."""
    fams = set()
    for path in font_files(dirs):
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = (path, st.st_size, st.st_mtime_ns)
        if key not in _family_cache:
            _family_cache[key] = frozenset(read_font(path)[0])
        fams |= _family_cache[key]
    return fams


# --- which directories ---------------------------------------------------------------------------

def _split(value):
    if not value:
        return []
    return [os.path.abspath(os.path.expanduser(p.strip())) for p in str(value).split(os.pathsep) if p.strip()]


def check_font_dirs(font_dirs):
    """The ``font_dirs`` launch option as a list of absolute paths. Raises TypeError for a wrong type,
    and on Linux ValueError for a path that is not a directory: a typo must not silently fall back to
    the lookalikes. (Elsewhere the option does nothing: Windows and macOS have their own fonts.)"""
    if font_dirs is None:
        return []
    if isinstance(font_dirs, (str, os.PathLike)):
        font_dirs = [font_dirs]
    if not isinstance(font_dirs, (list, tuple)) or not all(isinstance(d, (str, os.PathLike)) for d in font_dirs):
        raise TypeError("font_dirs must be a directory path or a list of them")
    dirs = [os.path.abspath(os.path.expanduser(os.fspath(d))) for d in font_dirs]
    if sys.platform == "linux":
        for d in dirs:
            if not os.path.isdir(d):
                raise ValueError(f"font_dirs: {d} is not a directory")
    return dirs


def resolve_font_dirs(font_dirs=None, environ=None):
    """``(user_dirs, fallback_dirs, ignored)``: the option's directories then CLEARCOTE_FONT_DIRS, and
    CLEARCOTE_FALLBACK_FONT_DIRS, each without duplicates; ``ignored`` lists entries from the
    environment that are not directories (the option raises for those instead)."""
    environ = os.environ if environ is None else environ
    user, fallback, ignored = [], [], []
    for d in check_font_dirs(font_dirs):
        if d not in user:
            user.append(d)
    for d in _split(environ.get(FONT_DIRS_ENV)):
        if not os.path.isdir(d):
            ignored.append(d)
        elif d not in user:
            user.append(d)
    for d in _split(environ.get(FALLBACK_FONT_DIRS_ENV)):
        if not os.path.isdir(d):
            ignored.append(d)
        elif d not in user and d not in fallback:
            fallback.append(d)
    return user, fallback, ignored


# --- the generated fontconfig file ---------------------------------------------------------------

def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _unxml(s):
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&").strip()


_RULE = re.compile(
    r'[ \t]*<match\b[^>]*>\s*<test\b[^>]*name="family"[^>]*>\s*<string>([^<]*)</string>\s*</test>\s*'
    r'<edit\b[^>]*name="family"[^>]*>\s*<string>([^<]*)</string>(\s*</edit>\s*</match>)[ \t]*\n?')
_ALIAS = re.compile(r'(<alias\b[^>]*>\s*<family>([^<]*)</family>\s*<prefer>\s*<family>)([^<]*)(</family>)')
_BUNDLE_DIR = re.compile(r"<dir>\s*@FONTS_DIR@\s*</dir>")


def genuine_rules(template, genuine):
    """``template`` with the lookalike rules and generics adjusted to the ``genuine`` families (a set
    of lower-cased names your own fonts provide). A family that is genuinely present loses its
    rule; a non-Windows name whose Windows family is present (Helvetica -> Arial) and a generic that
    resolves to it on Windows (sans-serif -> Arial) point at the genuine family."""
    if not genuine:
        return template

    def rule(m):
        family, clone = _unxml(m.group(1)), _unxml(m.group(2))
        if family.lower() in genuine:
            return ""
        target = _WINDOWS_ALIASES.get(family.lower())
        if target and target.lower() in genuine and clone != target:
            return m.group(0).replace(f"<string>{m.group(2)}</string>{m.group(3)}",
                                      f"<string>{_xml(target)}</string>{m.group(3)}")
        return m.group(0)

    def alias(m):
        target = _GENERIC_WINDOWS.get(_unxml(m.group(2)).lower())
        if target and target.lower() in genuine:
            return m.group(1) + _xml(target) + m.group(4)
        return m.group(0)

    return _ALIAS.sub(alias, _RULE.sub(rule, template))


def build_conf(template, fonts_dir, cache_dir, user_dirs=(), fallback_dirs=(), genuine=frozenset()):
    """The fontconfig file for one launch: the template with its directories filled in, your own
    directories ahead of the bundle, the fallback ones after it, and the rules adjusted to the
    families your own fonts genuinely provide."""
    conf = genuine_rules(template, genuine)
    if user_dirs or fallback_dirs:
        dirs = [f"<dir>{_xml(d)}</dir>" for d in user_dirs] + ["<dir>@FONTS_DIR@</dir>"] \
            + [f"<dir>{_xml(d)}</dir>" for d in fallback_dirs]
        if _BUNDLE_DIR.search(conf):
            conf = _BUNDLE_DIR.sub(lambda _m: "\n  ".join(dirs), conf, count=1)
        else:  # a template without the usual line: still list everything, the bundle's place unknown
            conf = conf.replace("</fontconfig>", "  " + "\n  ".join(dirs[:len(user_dirs)] + dirs[len(user_dirs) + 1:])
                                + "\n</fontconfig>")
    return conf.replace("@FONTS_DIR@", fonts_dir).replace("@CACHE_DIR@", cache_dir)


def _write_atomic(path, text):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def engine_honours_genuine_faces(exe_path):
    """Whether the engine at ``exe_path`` lets a genuine face from your own fonts win (see
    GENUINE_FACES_SWITCH)."""
    from ._launchopts import engine_supports_switch
    return engine_supports_switch(exe_path, GENUINE_FACES_SWITCH)


def genuine_faces_disabled(args):
    """Whether the launch turns genuine faces off itself (``--disable-genuine-font-faces`` in ``args``).
    The engine then keeps every substitute, so the rules must stay as the template has them: measured on
    an engine with the switch, retargeting anyway sent Helvetica to the genuine file while Arial stayed
    on its substitute."""
    flag = "--" + GENUINE_FACES_SWITCH
    return any(isinstance(a, str) and (a == flag or a.startswith(flag + "=")) for a in args or ())


def linux_font_env(exe_path, font_dirs=None, args=()):
    """Return ``{"FONTCONFIG_FILE": ...}`` on Linux when the font bundle is present, else ``{}``.

    ``font_dirs`` (and CLEARCOTE_FONT_DIRS / CLEARCOTE_FALLBACK_FONT_DIRS) add directories: see the
    module docstring. Without any, the file is ``fonts.generated.conf`` exactly as before; with some,
    each distinct result gets its own ``fonts.generated-<hash>.conf``, so launches with different
    directories never rewrite each other's file. ``args`` is the engine's command line (see
    genuine_faces_disabled)."""
    if sys.platform != "linux":
        return {}
    user_dirs, fallback_dirs, _ignored = resolve_font_dirs(font_dirs)
    fonts_dir = os.path.join(os.path.dirname(exe_path), "fonts")
    template = os.path.join(fonts_dir, "fonts.conf.template")
    if not os.path.isfile(template):
        return {}
    try:
        cache_dir = os.path.join(tempfile.gettempdir(), "cc-fc-cache")
        os.makedirs(cache_dir, exist_ok=True)
        with open(template, "r", encoding="utf-8") as fh:
            text = fh.read()
        # The rules follow your fonts only on an engine that draws them (GENUINE_FACES_SWITCH).
        genuine = (dir_families(user_dirs) if user_dirs and engine_honours_genuine_faces(exe_path)
                   and not genuine_faces_disabled(args) else frozenset())
        conf = build_conf(text, fonts_dir, cache_dir, user_dirs, fallback_dirs, genuine)
        name = "fonts.generated.conf"
        if user_dirs or fallback_dirs:
            name = "fonts.generated-%s.conf" % hashlib.sha256(conf.encode("utf-8")).hexdigest()[:12]
        conf_path = os.path.join(fonts_dir, name)
        _write_atomic(conf_path, conf)
        return {"FONTCONFIG_FILE": conf_path}
    except OSError:
        return {}  # never block a launch on font wiring


def linux_font_report(exe_path, font_dirs=None, environ=None):
    """What ``clearcote info`` says about fonts on Linux (the ``fonts`` key of its JSON): whether the
    build bundles fonts, which scripts the fonts a launch would see can draw, and which of the key
    Windows families your own fonts provide for real. ``None`` when the build ships no bundle."""
    fonts_dir = os.path.join(os.path.dirname(exe_path), "fonts")
    if not os.path.isfile(os.path.join(fonts_dir, "fonts.conf.template")):
        return None
    user_dirs, fallback_dirs, ignored = resolve_font_dirs(font_dirs, environ)
    wanted = [cp for _name, cp in SCRIPT_SAMPLES]
    covered, color = set(), set()
    for path in font_files([fonts_dir] + user_dirs + fallback_dirs):
        _fams, got, has_color = read_font(path, wanted)
        covered |= got
        if has_color:
            color |= got
    emoji = dict(SCRIPT_SAMPLES)["Emoji"]
    ok = [name for name, cp in SCRIPT_SAMPLES if cp in (color if cp == emoji else covered)]
    report = {
        "bundled": True,
        "note": "metric-compatible Windows font clones are bundled with this build",
        "scripts": {"covered": ok, "missing": [name for name, _cp in SCRIPT_SAMPLES if name not in ok]},
        "fontDirs": user_dirs,
        "fallbackFontDirs": fallback_dirs,
    }
    if ignored:
        report["ignoredFontDirs"] = ignored
    if user_dirs:
        genuine = dir_families(user_dirs)
        report["genuineWindowsFamilies"] = [f for f in KEY_WINDOWS_FAMILIES if f.lower() in genuine]
        report["lookalikeWindowsFamilies"] = [f for f in KEY_WINDOWS_FAMILIES if f.lower() not in genuine]
        report["genuineFacesSupported"] = engine_honours_genuine_faces(exe_path)
    return report


def linux_locale_env(args, platform=None):
    """Return ``{"LANGUAGE": <ui locale>}`` on Linux when ``args`` pin a UI locale with ``--lang``.

    Chrome on Linux takes its UI locale (browser strings such as a form's validationMessage, and
    before r29 also Intl) from the environment -- LANGUAGE, LC_ALL, LC_MESSAGES, LANG, in GLib's
    order -- and engines before 153 r29 ignore ``--lang`` there, so a German persona on a Linux host
    still spoke English. LANGUAGE is read first and only steers message catalogues, so it fixes that
    without touching the C library locale (LANG/LC_* would also change number parsing and
    fontconfig's default language). ``de`` -> ``de``, ``en-GB`` -> ``en_GB``.
    """
    if (platform or sys.platform) != "linux":
        return {}
    lang = None
    for arg in args or ():
        if isinstance(arg, str) and arg.startswith("--lang="):
            lang = arg.split("=", 1)[1].strip()  # the last one wins, as in Chromium
    if not lang:
        return {}
    return {"LANGUAGE": lang.replace("-", "_")}


def apply_font_env(exe_path, pw_kwargs, args=(), font_dirs=None):
    """Merge the bundled-font FONTCONFIG_FILE (and, from ``args``, the Linux UI-locale LANGUAGE --
    see linux_locale_env) into ``pw_kwargs['env']`` for Playwright's launch.

    Playwright replaces the child env when ``env`` is set, so we include ``os.environ`` too.
    Precedence: os.environ < bundled fonts + locale < caller-supplied env. No-op when there's nothing
    to add (leaves ``pw_kwargs`` untouched so Playwright uses the default env).
    """
    font_env = dict(linux_font_env(exe_path, font_dirs, args))
    font_env.update(linux_locale_env(args))
    user_env = pw_kwargs.get("env")
    if not font_env and not user_env:
        return
    merged = dict(os.environ)
    merged.update(font_env)
    if user_env:
        merged.update(user_env)
    pw_kwargs["env"] = merged
