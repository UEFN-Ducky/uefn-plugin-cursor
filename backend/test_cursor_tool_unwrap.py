"""Self-check: Cursor SDK mcp/read wrappers unwrap to real tool names."""

from __future__ import annotations

from cursor_tool_unwrap import unwrap_cursor_tool


def test_mcp_wrapper() -> None:
    name, args = unwrap_cursor_tool(
        "mcp",
        {
            "providerIdentifier": "uefn",
            "toolName": "workspace_list_verse_errors",
            "args": {"pretty": False},
        },
    )
    assert name == "workspace_list_verse_errors"
    assert args == {"pretty": False}


def test_generic_tool_wrapper() -> None:
    name, args = unwrap_cursor_tool(
        "tool",
        {"toolName": "find_devices", "args": {"label_filter": "Player"}},
    )
    assert name == "find_devices"
    assert args == {"label_filter": "Player"}


def test_builtin_read_passthrough() -> None:
    name, args = unwrap_cursor_tool("read", {"path": "a.verse"})
    assert name == "read"
    assert args == {"path": "a.verse"}


def test_generic_tool_without_inner_stays_tool() -> None:
    name, args = unwrap_cursor_tool("tool", {"path": "C:/proj/Content/Verse"})
    assert name == "tool"
    assert args == {"path": "C:/proj/Content/Verse"}


if __name__ == "__main__":
    test_mcp_wrapper()
    test_generic_tool_wrapper()
    test_builtin_read_passthrough()
    test_generic_tool_without_inner_stays_tool()
    print("ok")
