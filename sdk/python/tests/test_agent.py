from clearcote._agent import AGENT_KEYS, OPENROUTER_BASE_URL, agent_args


def test_agent_off_by_default():
    assert agent_args({}) == []
    # a model alone does NOT enable the agent (needs a key or url)
    assert agent_args({"agent_model": "openai/gpt-4o-mini"}) == []


def test_key_enables_and_defaults_openrouter_url():
    args = agent_args({"agent_llm_key": "sk-or-1"})
    assert f"--agent-llm-url={OPENROUTER_BASE_URL}" in args
    assert "--agent-llm-key=sk-or-1" in args


def test_url_alone_enables_without_key():
    args = agent_args({"agent_llm_url": "http://localhost:1234/v1"})
    assert "--agent-llm-url=http://localhost:1234/v1" in args


def test_typing_defaults_to_human_when_agent_on():
    args = agent_args({"agent_llm_key": "k"})
    assert any(a.startswith("--enable-features=GlicActorIncrementalTyping:") for a in args)
    # the 'human' profile pushes the auto-paste threshold out so long text still types key-by-key
    assert any("long-text-paste-threshold/100000" in a for a in args)


def test_typing_instant_disables_incremental():
    args = agent_args({"agent_llm_key": "k", "agent_typing": "instant"})
    assert "--disable-features=GlicActorIncrementalTyping" in args


def test_typing_flag_absent_when_agent_off():
    # no key/url -> agent off -> no typing flag emitted
    assert agent_args({"agent_typing": "human"}) == []


def test_maps_every_agent_option():
    args = agent_args({
        "agent_llm_url": "http://h/v1",
        "agent_llm_key": "k",
        "agent_model": "openai/gpt-4o-mini",
        "agent_tool_mode": "json",
    })
    for expected in (
        "--agent-llm-url=http://h/v1",
        "--agent-llm-key=k",
        "--agent-model=openai/gpt-4o-mini",
        "--agent-tool-mode=json",
    ):
        assert expected in args


def test_agent_keys_constant():
    assert set(AGENT_KEYS) == {
        "agent_llm_url", "agent_llm_key", "agent_model", "agent_tool_mode", "agent_typing"}


# -- run_agent_task: which CDP session carries Browser.agentRunTask ---------------------------------

class _Session:
    def __init__(self, name, log):
        self.name, self.log = name, log

    def send(self, method, params=None):
        self.log.append((self.name, method, params))
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "T1"}}
        return {"success": True, "finalText": "done", "stepsJson": '[{"action":"click"}]'}


class _ASession(_Session):
    async def send(self, method, params=None):
        return _Session.send(self, method, params)


def _fake_page(with_browser, session_cls=_Session):
    log = []

    def make(name):
        if session_cls is _Session:
            return session_cls(name, log)

        async def opened():  # the async API awaits session creation too
            return session_cls(name, log)
        return opened()

    class Browser:
        def new_browser_cdp_session(self):
            return make("browser")

    class Context:
        # Older Playwright clients: a persistent context has no Browser.
        browser = Browser() if with_browser else None

        def new_cdp_session(self, page):
            return make("page")

    class Page:
        context = Context()

    return Page(), log


def test_run_agent_task_uses_the_browser_session_when_there_is_one():
    from clearcote._agent import run_agent_task
    page, log = _fake_page(True)
    r = run_agent_task(page, "goal", max_steps=3)
    assert log[-1] == ("browser", "Browser.agentRunTask", {"targetId": "T1", "goal": "goal", "maxSteps": 3})
    assert r == {"success": True, "finalText": "done", "steps": [{"action": "click"}],
                 "stepsJson": '[{"action":"click"}]'}


def test_run_agent_task_falls_back_to_the_page_session_without_a_browser():
    from clearcote._agent import run_agent_task
    page, log = _fake_page(False)
    r = run_agent_task(page, "goal", model="m")
    assert log == [("page", "Target.getTargetInfo", None),
                   ("page", "Browser.agentRunTask", {"targetId": "T1", "goal": "goal", "model": "m"})]
    assert r["success"] is True


def test_run_agent_task_async_falls_back_to_the_page_session_without_a_browser():
    import asyncio

    from clearcote._agent import run_agent_task_async
    page, log = _fake_page(False, _ASession)
    r = asyncio.run(run_agent_task_async(page, "goal", plan_json="{}"))
    assert log[-1] == ("page", "Browser.agentRunTask", {"targetId": "T1", "goal": "goal", "planJson": "{}"})
    assert r["finalText"] == "done"
    page, log = _fake_page(True, _ASession)
    asyncio.run(run_agent_task_async(page, "goal"))
    assert log[-1][0] == "browser"
