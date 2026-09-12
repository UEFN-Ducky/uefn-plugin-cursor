"""Self-check: Effort dial maps onto Cursor.models.list parameter ids."""

from __future__ import annotations

from cursor_effort import model_params, pick_level, row_supports_thinking_effort

CLAUDE = {
    "id": "claude-sonnet-5",
    "params": {
        "thinking": ["false", "true"],
        "context": ["300k", "1m"],
        "effort": ["low", "medium", "high", "xhigh", "max"],
    },
}
GPT = {
    "id": "gpt-5.5",
    "params": {
        "context": ["272k", "1m"],
        "reasoning": ["none", "low", "medium", "high", "extra-high"],
        "fast": ["false", "true"],
    },
}
KIMI = {
    "id": "kimi-k3",
    "params": {"reasoning": ["low", "high", "max"]},
}
AUTO = {"id": "auto"}


def test_pick_level() -> None:
    assert pick_level(["low", "medium", "high", "xhigh", "max"], "off") is None
    assert pick_level(["none", "low", "medium", "high"], "off") == "none"
    assert pick_level(["low", "high", "max"], "medium") == "high"
    assert pick_level(["high", "max"], "low") == "high"


def test_claude_params() -> None:
    off = model_params(CLAUDE, "off")
    assert {"id": "thinking", "value": "false"} in off
    assert not any(p["id"] == "effort" for p in off)
    high = {p["id"]: p["value"] for p in model_params(CLAUDE, "high")}
    assert high["thinking"] == "true"
    assert high["effort"] == "high"


def test_gpt_off_uses_none() -> None:
    off = {p["id"]: p["value"] for p in model_params(GPT, "off")}
    assert off == {"reasoning": "none"}


def test_kimi_medium_steps_up() -> None:
    mid = {p["id"]: p["value"] for p in model_params(KIMI, "medium")}
    assert mid == {"reasoning": "high"}


def test_auto_has_no_params() -> None:
    assert model_params(AUTO, "high") == []
    assert row_supports_thinking_effort(AUTO) is False
    assert row_supports_thinking_effort(CLAUDE) is True


if __name__ == "__main__":
    test_pick_level()
    test_claude_params()
    test_gpt_off_uses_none()
    test_kimi_medium_steps_up()
    test_auto_has_no_params()
    print("ok")
