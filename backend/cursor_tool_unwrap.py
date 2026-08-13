"""Flatten Cursor SDK ToolCall wrappers into a Ducky tool name + args.

@cursor/sdk onDelta `toolCall` is a discriminated union
(`type: "read"|"edit"|"mcp"|…`, `args`), not `{name}`. MCP nests the real
tool as `args.toolName` + `args.args`. Keep this module free of app imports
so the self-check can run from the plugin folder.
"""

from __future__ import annotations

import json
from typing import Any

_MCP_WRAPS = frozenset({"callmcptool", "call_mcp_tool", "mcp", "tool"})


def unwrap_cursor_tool(name: str, args: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    n = (name or "").strip() or "tool"
    a = dict(args) if isinstance(args, dict) else {}
    if n.lower() not in _MCP_WRAPS:
        return n, a
    inner = str(a.get("toolName") or a.get("tool_name") or "").strip()
    if not inner:
        return n, a
    nested = a.get("args") if "args" in a else a.get("arguments")
    inner_args: dict[str, Any] = {}
    if isinstance(nested, dict):
        inner_args = dict(nested)
    elif isinstance(nested, str) and nested.strip():
        try:
            parsed = json.loads(nested)
            if isinstance(parsed, dict):
                inner_args = parsed
        except json.JSONDecodeError:
            pass
    return inner, inner_args
