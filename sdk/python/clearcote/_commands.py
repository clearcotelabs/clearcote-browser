"""clearcote -- manage the browser binary, diagnose a setup, save a licence key, run a CDP endpoint.

    clearcote install [--version 152] [--channel preview]   download + verify the binary
    clearcote info    [--quick] [--json] [--proxy URL]      diagnostics (alias: doctor)
    clearcote update  [--channel preview]                   fetch a newer build if one exists
    clearcote clear-cache                                   delete every cached binary
    clearcote login   [key]                                 save a licence key (validated first)
    clearcote logout                                        remove the saved key
    clearcote serve   [--port 9222] [--idle-timeout 300] ...  multi-identity CDP endpoint
    clearcote cloud   run|sessions|stop|events|recording|profile sync|webhooks ...  the hosted API
    clearcote version

``info`` never downloads: it reports what is already cached and what a launch would resolve to.
Mirrors the Node SDK's ``clearcote`` command. (``clearcote-agent`` and ``clearcote-serve`` are
separate, older entry points and are unchanged.)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

USAGE = """clearcote {version} -- manage and diagnose the Clearcote browser.

USAGE
  clearcote install [--version <v>] [--channel stable|preview]
  clearcote info [--quick] [--json] [--proxy <url>]      (alias: doctor)
  clearcote update [--channel stable|preview]
  clearcote clear-cache
  clearcote login [key]
  clearcote logout
  clearcote serve [--port 9222] [--host 127.0.0.1] [--idle-timeout <s>] [--data-dir <dir>]
                  [--max-browsers 16] [--allow-origin <origin>]... [--allow-host <name>]... [--headed]
                  [--fingerprint <seed>] [--platform <os>] [--proxy <url>] [--timezone <tz>]
                  [--accept-language <l>] [--geoip]
  clearcote cloud run <task> [--url <url>] [--schema <file.json>] [--secret <name>=<value>]... [--json]
  clearcote cloud sessions | stop <id> | events <id> | recording <id> [-o <file.mp4>]
  clearcote cloud profile sync <name> (--from-profile <dir> | --from-cdp <url> | --from-file <file> | --login <url>)
  clearcote cloud webhooks add <url> | list | rm <id> | test <id>      (all flags: clearcote cloud --help)
  clearcote version

INFO FLAGS
  --quick          skip everything that needs the network or a launch (seat count, launch test)
  --json           machine-readable output
  --proxy <url>    resolve the exit IP, timezone and language a launch through this proxy would use

ENVIRONMENT
  CLEARCOTE_LICENSE_KEY, CLEARCOTE_RELEASE_CHANNEL, CLEARCOTE_GEOIP_TIMEOUT_SECONDS,
  CLEARCOTE_LICENSE_THROUGH_PROXY, CLEARCOTE_BINARY, CLEARCOTE_CACHE, CLEARCOTE_SERVE_IDLE_TIMEOUT,
  CLEARCOTE_API_KEY, CLEARCOTE_API_URL, CLEARCOTE_CLOUD"""

# `clearcote cloud --help`. Kept byte-identical to CLOUD_USAGE in the Node SDK's cli-commands.ts.
CLOUD_USAGE = """clearcote cloud -- the hosted Clearcote API from the command line.

USAGE
  clearcote cloud run <task> [--url <url>] [--schema <file.json>] [--secret <name>=<value>]...
                      [--secret-domain <name>=<host>]... [--handoff] [--record] [--json]
                      [--country <cc>] [--state <s>] [--city <c>] [--proxy managed|<url>]
                      [--profile <name> [--persist-profile]] [--fingerprint <seed>]
                      [--timeout-sec <n>] [--max-steps <n>] [--note <text>]
  clearcote cloud sessions [--json]
  clearcote cloud stop <id> [--json]
  clearcote cloud events <id> [--json]
  clearcote cloud recording <id> [-o <file.mp4>] [--json]
  clearcote cloud profile sync <name> (--from-profile <dir> | --from-cdp <url> | --from-file <state.json>
                      | --login <url>) (--domain <domain>... | --all-domains) [--replace] [--json]
  clearcote cloud webhooks add <url> [--event <type>]... [--json]
  clearcote cloud webhooks list [--json]
  clearcote cloud webhooks rm <id> [--json]
  clearcote cloud webhooks test <id> [--json]

  run waits for the result and exits 0 only when the run succeeded. profile sync uploads only the
  cookies of the --domain names you list (subdomains included); --all-domains uploads every cookie.

ENVIRONMENT
  CLEARCOTE_API_KEY (required), CLEARCOTE_API_URL (default https://www.clearcotelabs.com)"""


class CliExit(SystemExit):
    """Raised by fail(): a SystemExit carrying the message already written to stderr."""


def _sdk_version():
    from . import __version__
    return __version__


def usage():
    return USAGE.format(version=_sdk_version())


def _out(line=""):
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def fail(msg, code=1):
    sys.stderr.write(f"clearcote: {msg}\n")
    sys.stderr.flush()
    raise CliExit(code)


def _geo_db_path():
    from .geoip import geo_cache_root
    return os.path.join(geo_cache_root(), "geoip-aio-all.mmdb")


def _missing_shared_libs(exe):
    if not sys.platform.startswith("linux"):
        return []
    try:
        r = subprocess.run(["ldd", "--", exe], capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return []
    return [line.strip().split(" ")[0] for line in (r.stdout or "").splitlines() if "not found" in line]


def build_info(quick=False, proxy=None, launch_fn=None):
    """What ``clearcote info`` reports; also the ``--json`` shape (same keys as the Node CLI)."""
    import platform as _platform

    from ._launchopts import GATED_ENGINE_SWITCHES, engine_supports_switch
    from ._license import get_session_seats, license_key_source
    from .download import list_cached_builds, resolve_release_channel
    from .release import RELEASE

    src = license_key_source()
    try:
        channel = resolve_release_channel()
    except ValueError as e:
        channel = f"invalid ({e})"
    cached = list_cached_builds()
    env_binary = os.environ.get("CLEARCOTE_BINARY")
    pick = {"path": env_binary} if env_binary else (cached[0] if cached else None)
    license_info = {"source": src["source"]}
    if src.get("masked"):
        license_info["key"] = src["masked"]
    binary = {"source": "CLEARCOTE_BINARY" if env_binary else ("cache" if pick else "none")}
    if pick:
        binary["path"] = pick["path"]
        if not env_binary:
            binary["tag"] = pick["tag"]
    binary.update({"cached": cached, "pinnedFree": f"{RELEASE['version']} ({RELEASE['tag']})",
                   "releaseChannel": channel})
    report = {
        "sdk": {"version": _sdk_version(), "python": _platform.python_version(),
                "platform": f"{sys.platform}-{_platform.machine().lower()}"},
        "license": license_info,
        "binary": binary,
        "geoip": {"databaseCached": os.path.exists(_geo_db_path()), "path": _geo_db_path()},
    }

    if pick and os.path.exists(pick["path"]):
        names = ["proxy-auth", "socks5-credentials", "socks5-udp"] + [s[2:] for s in GATED_ENGINE_SWITCHES]
        report["engineFeatures"] = {n: engine_supports_switch(pick["path"], n) for n in names}

    if sys.platform.startswith("linux") and pick:
        template = os.path.join(os.path.dirname(pick["path"]), "fonts", "fonts.conf.template")
        report["fonts"] = (
            {"bundled": True, "note": "metric-compatible Windows font clones are bundled with this build"}
            if os.path.exists(template) else
            {"bundled": False, "note": "this build ships no font bundle; a Windows persona on this host "
                                       "may render with Linux fonts"})

    if not quick and src["source"] != "none":
        report["license"]["seats"] = get_session_seats()

    if quick:
        report["launch"] = {"tested": False, "reason": "skipped (--quick)"}
    elif not pick:
        report["launch"] = {"tested": False, "reason": "no binary installed — run: clearcote install"}
    else:
        if launch_fn is None:
            from . import launch as launch_fn  # noqa: N806
        try:
            # cloud=False: this tests the LOCAL install, whatever CLEARCOTE_CLOUD says.
            b = launch_fn(executable_path=pick["path"], headless=True, quiet=True, ephemeral_profile=False,
                          cloud=False)
            try:
                version = b.version
                version = version() if callable(version) else version
            finally:
                b.close()
            report["launch"] = {"tested": True, "ok": True, "version": version}
        except Exception as e:  # noqa: BLE001
            entry = {"tested": True, "ok": False, "error": (str(e).splitlines() or [type(e).__name__])[0]}
            libs = _missing_shared_libs(pick["path"])
            if libs:
                entry["missingLibs"] = libs
            report["launch"] = entry

    if proxy:
        from .geoip import resolve_geo_detailed
        geo, reason, _ms = resolve_geo_detailed(proxy, quiet=True)
        report["geoip"]["proxy"] = (
            {"exitIp": geo.get("ip"), "country": geo.get("country"), "timezone": geo.get("timezone"),
             "acceptLanguage": geo.get("accept_language")}
            if geo and geo.get("timezone") else {"error": reason})
    return report


def print_info(r):
    def yn(b):
        return "yes" if b else "no"

    _out(f"clearcote SDK   {r['sdk']['version']}  (python {r['sdk']['python']}, {r['sdk']['platform']})")
    lic = r["license"]
    _out("Licence         " + ("none (free build)" if lic["source"] == "none" else f"{lic.get('key')} from {lic['source']}"))
    seats = lic.get("seats")
    if seats:
        if seats["state"] == "ok":
            limit = seats.get("limit") if seats.get("limit") is not None else "unlimited"
            plan = f"  (plan: {seats['plan']})" if seats.get("plan") else ""
            _out(f"Seats           {seats['used']} of {limit} in use{plan}")
        else:
            _out(f"Seats           unavailable: {seats.get('reason') or seats['state']}")
    b = r["binary"]
    if b.get("path"):
        tag = f"  [{b['tag']}]" if b.get("tag") else ""
        _out(f"Binary          {b['path']}{tag} ({b['source']})")
    else:
        _out("Binary          not installed — run: clearcote install")
    _out(f"Release channel {b['releaseChannel']}")
    _out(f"Free pin        {b['pinnedFree']}")
    if len(b["cached"]) > 1:
        _out("Also cached     " + ", ".join(c["tag"] for c in b["cached"][1:]))
    if r.get("engineFeatures"):
        _out("Engine support  " + "  ".join(f"{k}={yn(v)}" for k, v in r["engineFeatures"].items()))
    la = r.get("launch")
    if la:
        if not la["tested"]:
            _out(f"Launch test     {la['reason']}")
        elif la.get("ok"):
            _out(f"Launch test     ok ({la['version']})")
        else:
            _out(f"Launch test     FAILED: {la['error']}")
            for lib in la.get("missingLibs") or []:
                _out(f"                missing library: {lib}")
    if r.get("fonts"):
        _out(f"Fonts           {r['fonts']['note']}")
    _out("GeoIP database  " + ("cached" if r["geoip"]["databaseCached"] else "not cached (downloaded on first geoip launch)"))
    p = r["geoip"].get("proxy")
    if p:
        _out(f"Proxy geo       FAILED: {p['error']}" if p.get("error") else
             f"Proxy geo       exit {p['exitIp']} ({p['country']})  timezone {p['timezone']}  language {p['acceptLanguage']}")


def _prompt_key():
    if not sys.stdin or not sys.stdin.isatty():
        fail("no key given. Run `clearcote login <key>`, or copy a key from "
             "https://www.clearcotelabs.com/dashboard/licenses")
    sys.stderr.write("Paste your licence key (https://www.clearcotelabs.com/dashboard/licenses): ")
    sys.stderr.flush()
    return (sys.stdin.readline() or "").strip()


_BUILD_DIR = __import__("re").compile(r"^(pro-.+|v\d.*)$")


def cache_build_dirs(root):
    """Browser build directories directly under the cache root: children holding a ``.verified``
    marker, or named like a build tag (``pro-*`` / ``v<digit>*``). The root itself, and anything
    else that may share it (CLEARCOTE_CACHE can point anywhere), is never touched."""
    out = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if os.path.islink(full) or not os.path.isdir(full):
            continue
        if os.path.exists(os.path.join(full, ".verified")) or _BUILD_DIR.match(name):
            out.append(full)
    return out


def _dir_size(p):
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _non_negative(name):
    def conv(v):
        try:
            n = float(v)
        except ValueError:
            n = -1
        if n < 0 or n != n:
            raise argparse.ArgumentTypeError(f"--{name} must be a non-negative number")
        return int(n) if n.is_integer() else n
    return conv


def _parser():
    ap = argparse.ArgumentParser(prog="clearcote", add_help=False)
    sub = ap.add_subparsers(dest="cmd")
    info = sub.add_parser("info", add_help=False, aliases=["doctor"])
    info.add_argument("--quick", action="store_true")
    info.add_argument("--no-launch", action="store_true")
    info.add_argument("--json", action="store_true")
    info.add_argument("--proxy")
    for name in ("install", "update"):
        p = sub.add_parser(name, add_help=False)
        p.add_argument("--version")
        p.add_argument("--channel")
    sub.add_parser("clear-cache", add_help=False)
    login = sub.add_parser("login", add_help=False)
    login.add_argument("key", nargs="?")
    sub.add_parser("logout", add_help=False)
    sub.add_parser("version", add_help=False)
    s = sub.add_parser("serve", add_help=False)
    s.add_argument("--port", type=_non_negative("port"), default=9222)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--idle-timeout", type=_non_negative("idle-timeout"))
    s.add_argument("--data-dir")
    s.add_argument("--max-browsers", type=_non_negative("max-browsers"))
    s.add_argument("--allow-origin", action="append")
    s.add_argument("--allow-host", action="append")
    s.add_argument("--headed", action="store_true")
    s.add_argument("--fingerprint")
    s.add_argument("--platform")
    s.add_argument("--proxy")
    s.add_argument("--timezone")
    s.add_argument("--accept-language")
    s.add_argument("--geoip", action="store_true")
    s.add_argument("--quiet", action="store_true")
    return ap


# ── clearcote cloud ──────────────────────────────────────────────────────────────────────────────
# Output is part of the contract: tests/test_parity_cloud_cli.py runs the Node CLI against the same
# fake API and compares stdout byte for byte. Change a line here, change it in cli-commands.ts too.

def _dump(obj):
    _out(json.dumps(obj, indent=2, ensure_ascii=False))


def _s(v):
    return "-" if v is None else str(v)


def _eur(v, places):
    return f"€{v:.{places}f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "-"


def format_run(run):
    """The human summary `clearcote cloud run` prints."""
    lines = [f"run {_s(run.get('id'))} {_s(run.get('status'))}"]
    r = run.get("result") or {}
    if r.get("status"):
        lines.append(f"result  {r['status']}" + (f": {r['detail']}" if r.get("detail") else ""))
    if r.get("url"):
        lines.append(f"url     {r['url']}")
    if r.get("title"):
        lines.append(f"title   {r['title']}")
    if r.get("outputError"):
        lines.append(f"output  error: {r['outputError']}")
    elif r.get("output") is not None:
        lines.append("output  " + json.dumps(r["output"], indent=2, ensure_ascii=False))
    cost = run.get("costEur")
    if isinstance(cost, dict) and cost.get("total") is not None:
        lines.append(f"cost    {_eur(cost['total'], 4)}")
    return lines


def _session_line(s):
    line = f"{_s(s.get('id'))}  {_s(s.get('status'))}  {_s(s.get('createdAt'))}  {_eur(s.get('costEur'), 4)}"
    return line + (f"  {s['note']}" if s.get("note") else "")


# A --secret argument is never echoed back, not even a malformed one: it may be the bare secret.
SECRET_SHAPE = "--secret wants <name>=<value> (the argument given is not shown: it may hold the secret)"


def _parse_pairs(values, flag):
    out = []
    for raw in values or []:
        name, sep, value = raw.partition("=")
        if not sep or not name.strip() or not value:
            fail(SECRET_SHAPE if flag == "--secret" else f"{flag} wants <name>=<value>, got '{raw}'", 2)
        out.append((name.strip(), value))
    return out


def _secret_args(argv):
    """Every --secret argument on the command line (``--secret x`` and ``--secret=x``), so an
    argument error can be kept from printing one. ``--secret name value`` (a space typed for the
    ``=``) counts the word after the name too: that is the value."""
    found = []
    for i, tok in enumerate(argv):
        if tok == "--secret" and i + 1 < len(argv):
            found.append(argv[i + 1])
            if "=" not in argv[i + 1] and i + 2 < len(argv):
                found.append(argv[i + 2])
        elif tok.startswith("--secret="):
            found.append(tok[len("--secret="):])
    return [s for raw in found for s in (raw, raw.partition("=")[2]) if s]


def cloud_secrets(secret_args, domain_args):
    """--secret name=value and --secret-domain name=host as the API's ``secrets`` object: a plain
    string, or ``{value, domains}`` once a domain is given for that name."""
    secrets = dict(_parse_pairs(secret_args, "--secret"))
    domains = {}
    for name, host in _parse_pairs(domain_args, "--secret-domain"):
        if name not in secrets:
            fail(f"--secret-domain {name}: there is no --secret {name}=...", 2)
        domains.setdefault(name, []).append(host.strip().lower())
    return {n: ({"value": v, "domains": domains[n]} if n in domains else v) for n, v in secrets.items()}


# `clearcote cloud run` browser/run options that take a value. Each maps to the Runs.create keyword
# of the same name (the Python launch kwargs): --timeout-sec -> timeout_sec -> timeoutSec, ...
RUN_OPTION_FLAGS = ("--country", "--state", "--city", "--proxy", "--profile", "--fingerprint",
                    "--timeout-sec", "--max-steps", "--note")


def _whole(value, flag):
    if value is None:
        return None
    if not (value.isascii() and value.isdigit()):
        fail(f"{flag} wants a whole number, got '{value}'", 2)
    return int(value)


def cloud_run_options(a):
    """The browser and run options of ``clearcote cloud run`` as Runs.create keywords (unset ones
    left out). ``--profile NAME`` loads a cloud profile; with ``--persist-profile`` the run also
    saves it back."""
    if a.persist_profile and not a.profile:
        fail("--persist-profile needs --profile <name>", 2)
    opts = {
        "country": a.country, "state": a.state, "city": a.city, "proxy": a.proxy,
        "profile": ({"name": a.profile, "persist": True} if a.persist_profile else a.profile) if a.profile else None,
        "fingerprint": a.fingerprint, "timeout_sec": _whole(a.timeout_sec, "--timeout-sec"),
        "max_steps": _whole(a.max_steps, "--max-steps"), "note": a.note,
        "handoff": True if a.handoff else None, "record": True if a.record else None,
    }
    return {k: v for k, v in opts.items() if v is not None}


def _run_progress(view):
    if view.get("status") == "waiting_for_human":
        from .cloud import _announce_handoff
        _announce_handoff(view)
    else:
        sys.stderr.write(f"[clearcote] run {view.get('id')}: {view.get('status')}\n")
        sys.stderr.flush()


def _cloud_parser():
    ap = argparse.ArgumentParser(prog="clearcote cloud", add_help=False, allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", add_help=False, allow_abbrev=False)
    ap.run_parser = run  # parsed on its own: the task words may sit between the flags, as in Node
    run.add_argument("task", nargs="+")
    run.add_argument("--url")
    run.add_argument("--schema")
    run.add_argument("--secret", action="append")
    run.add_argument("--secret-domain", action="append")
    run.add_argument("--handoff", action="store_true")
    run.add_argument("--record", action="store_true")
    run.add_argument("--json", action="store_true")
    for flag in RUN_OPTION_FLAGS:
        run.add_argument(flag)
    run.add_argument("--persist-profile", action="store_true")
    sub.add_parser("sessions", add_help=False, allow_abbrev=False).add_argument("--json", action="store_true")
    for name in ("stop", "events"):
        p = sub.add_parser(name, add_help=False, allow_abbrev=False)
        p.add_argument("id")
        p.add_argument("--json", action="store_true")
    rec = sub.add_parser("recording", add_help=False, allow_abbrev=False)
    rec.add_argument("id")
    rec.add_argument("-o", "--output")
    rec.add_argument("--json", action="store_true")
    prof = sub.add_parser("profile", add_help=False, allow_abbrev=False).add_subparsers(dest="action")
    sync = prof.add_parser("sync", add_help=False, allow_abbrev=False)
    sync.add_argument("name")
    sync.add_argument("--from-profile")
    sync.add_argument("--from-cdp")
    sync.add_argument("--from-file")
    sync.add_argument("--login")
    sync.add_argument("--domain", action="append")
    sync.add_argument("--all-domains", action="store_true")
    sync.add_argument("--replace", action="store_true")
    sync.add_argument("--json", action="store_true")
    hooks = sub.add_parser("webhooks", add_help=False, allow_abbrev=False).add_subparsers(dest="action")
    add = hooks.add_parser("add", add_help=False, allow_abbrev=False)
    add.add_argument("url")
    add.add_argument("--event", action="append")
    add.add_argument("--json", action="store_true")
    hooks.add_parser("list", add_help=False, allow_abbrev=False).add_argument("--json", action="store_true")
    for name in ("rm", "test"):
        p = hooks.add_parser(name, add_help=False, allow_abbrev=False)
        p.add_argument("id")
        p.add_argument("--json", action="store_true")
    return ap


def _no_exit(parser, secrets=()):
    """argparse exits on its own; route every (nested) parser's errors through fail(..., 2), and
    never let one print a --secret value ("unrecognized arguments: ..." lists raw arguments)."""
    def _error(message):
        if any(s in message for s in secrets):
            message = "invalid arguments (not shown: they include a --secret value)"
        fail(message, 2)
    parser.error = _error
    group = parser._subparsers
    for action in (group._group_actions if group else []):
        for sp in (getattr(action, "choices", None) or {}).values():
            _no_exit(sp, secrets)


def _cloud(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        _out(CLOUD_USAGE)
        return 0
    if argv[0] not in ("run", "sessions", "stop", "events", "recording", "profile", "webhooks"):
        fail(f"unknown cloud command '{argv[0]}'. Run `clearcote cloud --help`.", 2)
    parser = _cloud_parser()
    _no_exit(parser, _secret_args(argv))
    if argv[0] == "run":
        a = parser.run_parser.parse_intermixed_args(argv[1:])
        a.cmd = "run"
    else:
        a = parser.parse_args(argv)
    if a.cmd in ("profile", "webhooks") and not getattr(a, "action", None):
        fail(f"`clearcote cloud {a.cmd}` needs a subcommand. Run `clearcote cloud --help`.", 2)
    if a.cmd == "profile":
        sources = [s for s in (a.from_profile, a.from_cdp, a.from_file, a.login) if s]
        if len(sources) != 1:
            fail("profile sync needs exactly one of --from-profile, --from-cdp, --from-file or --login", 2)
        if a.domain and a.all_domains:
            fail("pass --domain or --all-domains, not both", 2)
        if not a.domain and not a.all_domains:
            fail("refusing to upload every cookie: pass --domain <domain> (repeatable, subdomains included) "
                 "or --all-domains", 2)
    secrets = cloud_secrets(a.secret, a.secret_domain) if a.cmd == "run" else None
    run_options = cloud_run_options(a) if a.cmd == "run" else None
    schema = None
    if a.cmd == "run" and a.schema:
        try:
            with open(a.schema, encoding="utf-8") as fh:
                schema = json.load(fh)
        except (OSError, ValueError) as e:
            fail(f"--schema {a.schema}: {e}", 2)

    from .cloud import Cloud, CloudError
    try:
        cloud = Cloud()
        return _cloud_command(cloud, a, secrets, schema, run_options)
    except CloudError as e:
        detail = f" (HTTP {e.status}{', ' + e.code if e.code else ''})" if e.status else ""
        fail(f"{e.message}{detail}")
    except ValueError as e:
        fail(str(e))
    return 1


def _cloud_command(cloud, a, secrets, schema, run_options=None):
    if a.cmd == "run":
        run = cloud.runs.create(" ".join(a.task), url=a.url, schema=schema, secrets=secrets or None,
                                on_update=_run_progress, **(run_options or {}))
        if a.json:
            _dump(run)
        else:
            for line in format_run(run):
                _out(line)
        return 0 if run.get("status") == "succeeded" else 1

    if a.cmd == "sessions":
        data = cloud.browsers.list()
        if a.json:
            _dump(data)
            return 0
        sessions = (data or {}).get("sessions") or []
        for s in sessions:
            _out(_session_line(s))
        if not sessions:
            _out("no sessions")
        if (data or {}).get("balanceEur") is not None:
            _out(f"balance {_eur(data['balanceEur'], 2)}")
        return 0

    if a.cmd == "stop":
        view = cloud.browsers.stop(a.id)
        if a.json:
            _dump(view)
        else:
            _out(f"stopped {a.id} ({_s((view or {}).get('status'))})")
        return 0

    if a.cmd == "events":
        events, after = [], 0
        while True:
            page = cloud.browsers.events(a.id, after=after) or {}
            batch = page.get("events") or []
            events.extend(batch)
            nxt = page.get("next")
            if not batch or nxt is None or nxt == after:
                break
            after = nxt
        if a.json:
            _dump({"events": events})
            return 0
        for e in events:
            data = e.get("data")
            extra = ("  " + json.dumps(data, ensure_ascii=False, separators=(",", ":"))) if data else ""
            _out(f"{_s(e.get('seq'))}  {_s(e.get('at'))}  {_s(e.get('type'))}{extra}")
        if not events:
            _out("no events")
        return 0

    if a.cmd == "recording":
        path = a.output or f"{a.id}.mp4"
        cloud.browsers.download_recording(a.id, path)
        size = os.path.getsize(path)
        if a.json:
            _dump({"path": path, "bytes": size})
        else:
            _out(f"saved {path} ({size} bytes)")
        return 0

    if a.cmd == "profile":
        res = cloud.profiles.sync(a.name, from_profile=a.from_profile, from_cdp=a.from_cdp,
                                  from_file=a.from_file, login_url=a.login, domains=a.domain,
                                  all_domains=a.all_domains, replace=a.replace) or {}
        if a.json:
            _dump(res)
            return 0
        _out(f"imported {_s(res.get('imported'))} cookies into profile {res.get('name') or a.name} "
             f"({_s(res.get('cookies'))} in total)")
        if res.get("domains"):
            _out("domains  " + ", ".join(res["domains"]))
        return 0

    # webhooks
    if a.action == "add":
        res = cloud.webhooks.create(a.url, events=a.event) or {}
        if a.json:
            _dump(res)
            return 0
        _out(f"webhook {_s(res.get('id'))} -> {_s(res.get('url'))}")
        _out("events  " + (", ".join(res.get("events") or []) or "all except ping"))
        _out(f"secret  {_s(res.get('secret'))}")
        _out("store the secret now: it is not shown again")
        return 0
    if a.action == "list":
        data = cloud.webhooks.list() or {}
        if a.json:
            _dump(data)
            return 0
        hooks = data.get("webhooks") or []
        for h in hooks:
            ld = h.get("lastDelivery")
            last = f"  last {_s(ld.get('status'))} {_s(ld.get('at'))}" if ld else ""
            _out(f"{_s(h.get('id'))}  {_s(h.get('url'))}  {','.join(h.get('events') or []) or 'all'}{last}")
        if not hooks:
            _out("no webhooks")
        return 0
    res = cloud.webhooks.delete(a.id) if a.action == "rm" else cloud.webhooks.test(a.id)
    if a.json:
        _dump(res)
    else:
        _out(f"removed {a.id}" if a.action == "rm" else f"sent a ping to {a.id}")
    return 0


def _run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        _out(usage())
        return 0
    if argv[0] in ("version", "--version"):
        _out(_sdk_version())
        return 0
    if argv[0] == "cloud":
        return _cloud(argv[1:])
    known = ("info", "doctor", "install", "update", "clear-cache", "login", "logout", "serve")
    if argv[0] not in known:
        fail(f"unknown command '{argv[0]}'. Run `clearcote --help`.", 2)

    parser = _parser()

    def _error(message):
        fail(message, 2)

    parser.error = _error
    for action in parser._subparsers._group_actions:  # noqa: SLF001
        for sp in action.choices.values():
            sp.error = _error
    a = parser.parse_args(argv)
    cmd = a.cmd

    if cmd in ("info", "doctor"):
        report = build_info(quick=bool(a.quick or a.no_launch), proxy=a.proxy)
        if a.json:
            _out(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print_info(report)
        return 0

    if cmd in ("install", "update"):
        from . import download
        from ._license import resolve_license_key
        from .download import resolve_release_channel
        channel = resolve_release_channel(a.channel)
        path = download(version=a.version, release_channel=channel, license_key=resolve_license_key(),
                        # update: re-resolve the newest build instead of reusing the SDK's pin (free);
                        # PRO always asks the server
                        auto_update=True if cmd == "update" else None)
        _out(path)
        return 0

    if cmd == "clear-cache":
        from .download import default_cache_root
        root = default_cache_root()
        if not os.path.isdir(root):
            _out(f"nothing to clear ({root} does not exist)")
            return 0
        builds = cache_build_dirs(root)
        if not builds:
            _out(f"nothing to clear (no browser builds in {root})")
            return 0
        size = 0
        for d in builds:
            size += _dir_size(d)
            shutil.rmtree(d, ignore_errors=True)
        _out(f"removed {len(builds)} build(s) from {root} ({size / 1e6:.0f} MB)")
        return 0

    if cmd == "login":
        from ._license import get_session_seats, save_license_key
        key = (a.key or "").strip() or _prompt_key()
        if not key:
            fail("empty key")
        seats = get_session_seats(license_key=key)
        if seats["state"] == "invalid":
            fail(f"the licence server rejected this key ({seats.get('reason')}). Nothing was saved.")
        where = save_license_key(key)
        _out(f"saved to {where}")
        if seats["state"] == "ok":
            limit = seats.get("limit") if seats.get("limit") is not None else "unlimited"
            plan = f", plan {seats['plan']}" if seats.get("plan") else ""
            _out(f"valid: {seats['used']} of {limit} seats in use{plan}")
        else:
            _out(f"note: could not confirm the key right now ({seats.get('reason')}); it was saved anyway")
        return 0

    if cmd == "logout":
        from ._license import license_key_path, remove_license_key
        _out(f"removed {license_key_path()}" if remove_license_key() else "no saved key")
        if os.environ.get("CLEARCOTE_LICENSE_KEY"):
            _out("note: CLEARCOTE_LICENSE_KEY is still set in this environment and will keep being used")
        return 0

    if cmd == "serve":
        import signal

        from ._multiplex import serve_multiplex
        from ._net import to_proxy_spec
        opts = {"port": a.port, "host": a.host, "idle_timeout": a.idle_timeout, "data_dir": a.data_dir,
                "allow_origins": a.allow_origin, "allow_hosts": a.allow_host, "headless": not a.headed,
                "quiet": a.quiet}
        if a.max_browsers is not None:
            opts["max_browsers"] = a.max_browsers
        for k in ("fingerprint", "platform", "timezone", "accept_language"):
            if getattr(a, k):
                opts[k] = getattr(a, k)
        if a.proxy:
            # Credentials are split out of the URL: a user:pass@ left in --proxy-server is rejected by
            # Chromium's proxy parser, which then goes DIRECT (the host's real IP).
            try:
                opts["proxy"] = to_proxy_spec(a.proxy)
            except ValueError as e:
                fail(str(e), 2)
        if a.geoip:
            opts["geoip"] = True
        srv = serve_multiplex(**opts)

        def stop(*_a):
            srv.close()

        signal.signal(signal.SIGINT, stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, stop)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.close()
        return 0

    fail(f"unknown command '{cmd}'. Run `clearcote --help`.", 2)
    return 2


def main(argv=None):
    """``clearcote`` console-script entry point. Returns the exit code."""
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        return _run(argv)
    except CliExit as e:
        return e.code
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"clearcote: {e}\n")
        return 1


def _console_main():
    sys.exit(main())


if __name__ == "__main__":
    _console_main()
