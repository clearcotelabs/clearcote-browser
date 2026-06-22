"""clearcote-agent — terminal wrapper around Clearcote's in-browser AI agent.

Launches a REGULAR-profile Clearcote (anti-detect Chromium) wired to an
OpenAI-compatible endpoint (OpenRouter by default) and runs natural-language
goals against a page via the SDK. The agent perceives the page, asks the LLM
what to do, and acts through Chrome's Actor framework.

Two modes:

* one-shot (default, when ``--goal`` is given): launch -> goto -> run -> print -> close.
* interactive REPL (``-i``): launch headed + persistent, then a prompt loop where
  each non-``:`` line runs as a goal against the current page (the session persists,
  so you can sign in by hand between goals).

The agent only works in a regular/persistent profile, so this always uses the
SDK's ``launch_agent`` (never the incognito ``launch``).
"""

import argparse
import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit


def _eprint(*args, **kwargs):
    """Print to stderr (logs go to stderr, results go to stdout)."""
    print(*args, file=sys.stderr, **kwargs)


def _fail(msg, code=2):
    """Print a friendly (stack-trace-free) error and exit with ``code``."""
    _eprint(f"clearcote-agent: {msg}")
    sys.exit(code)


def _resolve_key(cli_key):
    """--key, then OPENROUTER_API_KEY, then CLEARCOTE_AGENT_KEY."""
    return (
        cli_key
        or os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("CLEARCOTE_AGENT_KEY")
    )


def _default_profile_dir():
    return os.path.join(os.path.expanduser("~"), ".clearcote", "agent-profile")


def _parse_proxy(proxy_url):
    """Turn ``scheme://user:pass@host:port`` into a Playwright proxy dict.

    Returns ``{"server": "scheme://host:port", "username": ..., "password": ...}``
    with the userinfo split out (username/password omitted when absent).
    """
    parts = urlsplit(proxy_url)
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"could not parse proxy URL: {proxy_url!r}")
    netloc = parts.hostname
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    server = urlunsplit((parts.scheme, netloc, "", "", ""))
    proxy = {"server": server}
    if parts.username is not None:
        proxy["username"] = parts.username
    if parts.password is not None:
        proxy["password"] = parts.password
    return proxy


def _normalize_url(raw):
    """Validate/normalize a starting URL. A bare host like ``example.com`` is upgraded to
    ``https://`` so the scheme is optional. Returns the normalized URL; raises ``ValueError`` if it
    can't be parsed into scheme + host."""
    if raw == "about:blank":
        return raw
    parts = urlsplit(raw)
    if parts.scheme and parts.netloc:
        return raw
    upgraded = "https://" + raw
    if urlsplit(upgraded).netloc:
        return upgraded
    raise ValueError(f"invalid url: {raw!r}")


def _build_parser():
    p = argparse.ArgumentParser(
        prog="clearcote-agent",
        description=(
            "Run natural-language goals on a web page via Clearcote's in-browser AI agent. "
            "Launches a regular-profile anti-detect Chromium and drives it with an LLM."
        ),
        epilog=(
            "Examples:\n"
            "  clearcote-agent --goal \"Search for kittens\" --url https://duckduckgo.com\n"
            "  clearcote-agent -i --url https://example.com\n\n"
            "The OpenRouter key is read from --key, then $OPENROUTER_API_KEY, then "
            "$CLEARCOTE_AGENT_KEY."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--goal", help="the task to run (required in one-shot mode)")
    p.add_argument("--url", default="about:blank", help="starting page (default: about:blank)")
    p.add_argument(
        "-i", "--interactive", action="store_true", help="interactive REPL mode (always headed)"
    )
    p.add_argument(
        "--model", default="openai/gpt-4o", help="OpenRouter model slug (default: openai/gpt-4o)"
    )
    p.add_argument(
        "--key",
        help="OpenRouter API key (fallbacks: $OPENROUTER_API_KEY, $CLEARCOTE_AGENT_KEY)",
    )
    p.add_argument("--llm-url", dest="llm_url", help="OpenAI-compatible base URL (default: OpenRouter)")
    p.add_argument(
        "--tool-mode", dest="tool_mode", choices=["tools", "json"], help="agent tool mode"
    )
    p.add_argument("--max-steps", dest="max_steps", default=20, help="max agent steps (default: 20)")
    p.add_argument(
        "--profile",
        help="persistent profile dir (default: <home>/.clearcote/agent-profile)",
    )
    p.add_argument("--headless", action="store_true", help="run headless (default: headed)")
    p.add_argument(
        "--executable", help="path to the Clearcote chrome.exe (optional; SDK auto-downloads)"
    )
    p.add_argument("--fingerprint", help="anti-detect seed (optional)")
    p.add_argument(
        "--proxy", help="proxy URL, e.g. http://user:pass@host:port (optional)"
    )
    p.add_argument("--timezone", help="IANA timezone, e.g. America/New_York (optional)")
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_out",
        help="one-shot only: print the raw result object as JSON to stdout",
    )
    return p


def _coerce_max_steps(raw):
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"--max-steps must be an integer, got {raw!r}")
    if n < 1:
        raise ValueError(f"--max-steps must be >= 1, got {n}")
    return n


def _print_result(result, page, json_out):
    """Render a result dict for the human (or JSON, one-shot)."""
    if json_out:
        print(json.dumps(result))
        return
    status = "OK" if result.get("success") else "FAIL"
    final_text = result.get("finalText", "")
    print(f"{status}: {final_text}" if final_text else status)
    steps = result.get("steps") or []
    for i, step in enumerate(steps, 1):
        if isinstance(step, dict):
            action = step.get("action") or step.get("type") or step.get("tool") or "?"
            bits = []
            for k in ("selector", "text", "url", "value", "target", "reason"):
                v = step.get(k)
                if v:
                    bits.append(f"{k}={v}")
            st = step.get("status") or step.get("result")
            tail = (" " + " ".join(bits)) if bits else ""
            if st:
                tail += f" [{st}]"
            print(f"  {i}. {action}{tail}")
        else:
            print(f"  {i}. {step}")
    try:
        print(f"page: {page.url}")
    except Exception:  # noqa: BLE001
        pass


def _build_launch_kwargs(args, key, profile_dir, headless):
    kwargs = {
        "user_data_dir": profile_dir,
        "agent_llm_key": key,
        "agent_model": args.model,
        "headless": headless,
    }
    if args.llm_url:
        kwargs["agent_llm_url"] = args.llm_url
    if args.tool_mode:
        kwargs["agent_tool_mode"] = args.tool_mode
    if args.fingerprint:
        kwargs["fingerprint"] = args.fingerprint
    if args.timezone:
        kwargs["timezone"] = args.timezone
    if args.executable:
        kwargs["executable_path"] = args.executable
    if args.proxy:
        try:
            kwargs["proxy"] = _parse_proxy(args.proxy)
        except ValueError as exc:
            _fail(str(exc), code=2)
    return kwargs


def _run_one(page, goal, model, max_steps):
    """Print the running header (stderr), run the goal, return the result dict."""
    from clearcote import run_agent_task

    _eprint(f"goal: {goal}")
    _eprint(f"running ({model}, up to {max_steps} steps)...")
    return run_agent_task(page, goal, model=model, max_steps=max_steps)


REPL_HELP = """commands:
  :goto <url>    navigate the current page
  :url           print the current URL
  :model <slug>  change the model for subsequent goals
  :steps <n>     change max-steps
  :help          show this help
  :quit / :exit  close and exit (Ctrl-D / EOF also quits)
  <anything else> run as a goal on the current page"""


def _repl(page, model, max_steps, initial_goal=None):
    _eprint("clearcote-agent interactive session. Type :help for commands, :quit to exit.")
    # If a --goal was passed alongside -i, run it as the first goal before the prompt loop.
    if initial_goal:
        try:
            result = _run_one(page, initial_goal, model, max_steps)
            _print_result(result, page, json_out=False)
        except Exception as exc:  # noqa: BLE001
            _eprint(f"agent error: {exc}")
    while True:
        try:
            line = input("agent> ")
        except (EOFError, KeyboardInterrupt):
            _eprint("")
            return 0
        line = line.strip()
        if not line:
            continue
        if line.startswith(":"):
            parts = line[1:].split(None, 1)
            cmd = parts[0].lower() if parts else ""
            arg = parts[1].strip() if len(parts) > 1 else ""
            if cmd in ("quit", "exit"):
                return 0
            if cmd == "help":
                _eprint(REPL_HELP)
            elif cmd == "url":
                try:
                    _eprint(page.url)
                except Exception as exc:  # noqa: BLE001
                    _eprint(f"could not read url: {exc}")
            elif cmd == "goto":
                if not arg:
                    _eprint("usage: :goto <url>")
                    continue
                try:
                    target = _normalize_url(arg)
                except ValueError as exc:
                    _eprint(str(exc))
                    continue
                try:
                    page.goto(target)
                    _eprint(f"navigated to {page.url}")
                except Exception as exc:  # noqa: BLE001
                    _eprint(f"navigation failed: {exc}")
            elif cmd == "model":
                if not arg:
                    _eprint(f"current model: {model}")
                else:
                    model = arg
                    _eprint(f"model set to {model}")
            elif cmd == "steps":
                try:
                    max_steps = _coerce_max_steps(arg)
                    _eprint(f"max-steps set to {max_steps}")
                except ValueError as exc:
                    _eprint(str(exc))
            else:
                _eprint(f"unknown command :{cmd} (try :help)")
            continue
        # any other line -> run as a goal on the current page
        try:
            result = _run_one(page, line, model, max_steps)
        except Exception as exc:  # noqa: BLE001
            _eprint(f"agent error: {exc}")
            continue
        _print_result(result, page, json_out=False)


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    key = _resolve_key(args.key)
    if not key:
        _fail(
            "no OpenRouter API key. Pass --key, or set $OPENROUTER_API_KEY or "
            "$CLEARCOTE_AGENT_KEY.",
            code=2,
        )

    try:
        max_steps = _coerce_max_steps(args.max_steps)
    except ValueError as exc:
        _fail(str(exc), code=2)

    interactive = args.interactive
    if not interactive and not args.goal:
        _fail("one-shot mode needs --goal (or use -i for an interactive session).", code=2)

    # Validate/normalize the starting URL early (about:blank passes; bare hosts upgrade to https://).
    if args.url:
        try:
            args.url = _normalize_url(args.url)
        except ValueError as exc:
            _fail(str(exc), code=2)

    headless = args.headless
    if interactive and headless:
        _eprint("clearcote-agent: ignoring --headless in interactive mode (always headed).")
        headless = False

    profile_dir = args.profile or _default_profile_dir()
    try:
        os.makedirs(profile_dir, exist_ok=True)
    except OSError as exc:
        _fail(f"could not create profile dir {profile_dir!r}: {exc}", code=1)

    try:
        from clearcote import launch_agent
    except Exception as exc:  # noqa: BLE001
        _fail(f"could not import the clearcote SDK: {exc}", code=1)

    launch_kwargs = _build_launch_kwargs(args, key, profile_dir, headless)

    ctx = None
    try:
        try:
            ctx = launch_agent(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001
            _fail(f"failed to launch Clearcote agent: {exc}", code=1)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if args.url and args.url != "about:blank":
            try:
                page.goto(args.url)
            except Exception as exc:  # noqa: BLE001
                _fail(f"failed to open {args.url!r}: {exc}", code=1)

        if interactive:
            return _repl(page, args.model, max_steps, initial_goal=args.goal)

        # one-shot
        try:
            result = _run_one(page, args.goal, args.model, max_steps)
        except Exception as exc:  # noqa: BLE001
            _fail(f"agent run failed: {exc}", code=1)
        _print_result(result, page, json_out=args.json_out)
        return 0 if result.get("success") else 1
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
