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
