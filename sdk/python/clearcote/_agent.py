"""In-browser AI agent (OpenRouter / any OpenAI-compatible endpoint).

    from clearcote import launch_agent, run_agent_task

    ctx = launch_agent(
        agent_llm_key="sk-or-...",          # turns the agent on
        agent_model="openai/gpt-4o-mini",   # any OpenRouter model slug
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://example.com")
    result = run_agent_task(page, "Click the 'More information...' link.", max_steps=8)
    print(result["success"], result["finalText"], result["steps"])
    ctx.close()

The agent runs *inside* the browser process: it perceives the live page, asks the LLM what to do,
and drives real (trusted) input through Chrome's Actor framework. Setting ``agent_llm_key`` (or
``agent_llm_url``) is all that's needed -- the engine auto-enables the Actor framework whenever
``--agent-llm-url`` is present, so no ``--enable-features``/``--disable-actor-safety-checks`` flags
are required. Requires a Clearcote binary that exposes ``Browser.agentRunTask``.

IMPORTANT -- the agent needs a REGULAR profile. Chrome's Actor framework only attaches to a normal
(persistent) profile, not an incognito one, so drive the agent from ``launch_agent()`` or
``launch_persistent_context()``. Plain ``launch()`` returns an incognito browser where the agent
can't attach the tab ("Actor service unavailable" / "could not attach tab to task").
"""

import json

# OpenRouter is the default base URL, so you can switch between any provider/model with a single
# ``agent_model`` slug.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# launch()/launch_persistent_context() kwargs that configure the agent (everything else passes
# straight through to Playwright).
AGENT_KEYS = (
    "agent_llm_url",
    "agent_llm_key",
    "agent_model",
    "agent_tool_mode",
)


def agent_args(opts):
    """Build the Chromium switches for the AI agent. Returns ``[]`` (agent off) unless a key/URL is set.

    Kwargs: ``agent_llm_url`` (default OpenRouter when a key is set), ``agent_llm_key`` (Bearer
    token -- setting it turns the agent on), ``agent_model`` (e.g. ``"openai/gpt-4o-mini"`` or any
    OpenRouter ``provider/model`` slug), ``agent_tool_mode`` (``"tools"`` or ``"json"``)."""
    args = []
    if opts.get("agent_llm_key") is None and opts.get("agent_llm_url") is None:
        return args
    args.append(f"--agent-llm-url={opts.get('agent_llm_url') or OPENROUTER_BASE_URL}")
    if opts.get("agent_llm_key") is not None:
        args.append(f"--agent-llm-key={opts['agent_llm_key']}")
    if opts.get("agent_model") is not None:
        args.append(f"--agent-model={opts['agent_model']}")
    if opts.get("agent_tool_mode") is not None:
        args.append(f"--agent-tool-mode={opts['agent_tool_mode']}")
    return args


def _page_target_id(page):
    session = page.context.new_cdp_session(page)
    info = session.send("Target.getTargetInfo")
    return info["targetInfo"]["targetId"]


def run_agent_task(page, goal, model=None, max_steps=None, plan_json=None):
    """Run an autonomous AI-agent task against ``page``.

    The engine perceives the page, asks the LLM (OpenRouter by default) what to do, and executes the
    actions until the goal is met or ``max_steps`` is reached. The browser must have been launched
    with ``agent_llm_key`` (and optionally ``agent_model``).

    ``model`` overrides the launch-time model for just this task. Returns a dict with keys
    ``success`` (bool), ``finalText`` (str), ``steps`` (parsed per-step journal) and ``stepsJson``
    (the raw string)."""
    browser = page.context.browser
    if browser is None:
        raise RuntimeError("run_agent_task: page is not attached to a Browser")
    session = browser.new_browser_cdp_session()
    params = {"targetId": _page_target_id(page), "goal": goal}
    if max_steps is not None:
        params["maxSteps"] = max_steps
    if model is not None:
        params["model"] = model
    if plan_json is not None:
        params["planJson"] = plan_json
    try:
        res = session.send("Browser.agentRunTask", params)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Browser.agentRunTask failed -- make sure this is a Clearcote build with the AI agent "
            "and that the browser was launched with agent_llm_key/agent_llm_url set. "
            f"Underlying error: {exc}"
        ) from exc
    try:
        steps = json.loads(res.get("stepsJson") or "[]")
    except ValueError:
        steps = []
    return {
        "success": bool(res.get("success")),
        "finalText": res.get("finalText", ""),
        "steps": steps,
        "stepsJson": res.get("stepsJson", "[]"),
    }
