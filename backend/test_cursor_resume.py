from __future__ import annotations

from pathlib import Path

src = Path(__file__).with_name("cursor_adapter.py").read_text(encoding="utf-8")


def test_followup_uses_agent_resume():
    assert "Agent.resume" in src
    assert "resume=True" in src
    assert "cfg.agentId" in src
