"""Regression tests for A2A consult toolset wiring."""


def test_a2a_consult_uses_separate_toolset_without_polluting_messaging():
    import tools.a2a_consult_tool  # noqa: F401 - registers the tool
    from toolsets import get_toolset

    messaging = get_toolset("messaging")
    a2a = get_toolset("a2a")

    assert messaging is not None
    assert a2a is not None
    assert "send_message" in messaging["tools"]
    assert "a2a_consult" not in messaging["tools"]
    assert "a2a_consult" in a2a["tools"]


def test_default_cli_toolset_still_recovers_messaging_after_a2a_registration(monkeypatch):
    import tools.a2a_consult_tool  # noqa: F401 - registers the tool
    from hermes_cli.tools_config import _get_platform_tools

    monkeypatch.delenv("HASS_TOKEN", raising=False)

    enabled = _get_platform_tools({}, "cli", include_default_mcp_servers=False)

    assert "messaging" in enabled
    assert "a2a" not in enabled
