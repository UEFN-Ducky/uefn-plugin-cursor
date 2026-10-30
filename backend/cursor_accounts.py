"""Hooks core's accounts call: run Cursor as one API key or one CLI sign-in.

``config_dir`` is the account's own folder (core's ``.../accounts/cursor/<id>``), None for
``default`` (the saved key / the CLI's own folder).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .cursor_adapter import ACCOUNT_DIR_ENV, which_cli


def _kind(config_dir: Any) -> str:
    from backend.agent.accounts import list_accounts

    aid = Path(config_dir).name
    return next((a["kind"] for a in list_accounts("cursor") if a["id"] == aid), "key")


def account_env(config_dir: Any) -> dict[str, str]:
    """Key account → its CURSOR_API_KEY; sign-in account → its CLI folder for the adapter."""
    from backend.agent.accounts import account_key

    if _kind(config_dir) == "login":
        return {ACCOUNT_DIR_ENV: str(config_dir)}
    return {"CURSOR_API_KEY": account_key("cursor", Path(config_dir).name) or ""}


def _agent_cli() -> str:
    cli = which_cli("agent", "") or which_cli("cursor-agent", "")
    if not cli:
        raise ValueError("Cursor sign-in needs the Cursor agent CLI (agent or cursor-agent) on PATH.")
    return cli


def _cli_env(config_dir: Any) -> dict[str, str]:
    return {"CURSOR_CONFIG_DIR": str(config_dir)} if config_dir else {}


def account_login(config_dir: Any) -> dict[str, Any]:
    return {"argv": [_agent_cli(), "login"], "env": _cli_env(config_dir)}


def _run_cli(config_dir: Any, *args: str) -> str:
    proc = subprocess.run([_agent_cli(), *args], env={**os.environ, **_cli_env(config_dir)},
                          capture_output=True, text=True, timeout=30)
    return f"{proc.stdout}\n{proc.stderr}"


def parse_status(text: str) -> dict[str, Any]:
    """`agent status` output → {loggedIn, email?}."""
    if re.search(r"not (?:logged|signed) in", text, re.IGNORECASE):
        return {"loggedIn": False}
    m = re.search(r"(?:logged|signed) in(?: as)?\s+(\S+@\S+)", text, re.IGNORECASE)
    if m:
        return {"loggedIn": True, "email": m.group(1).rstrip(".")}
    return {"loggedIn": bool(re.search(r"(?:logged|signed) in", text, re.IGNORECASE))}


def account_status(config_dir: Any) -> dict[str, Any]:
    try:
        return parse_status(_run_cli(config_dir, "status"))
    except Exception:
        return {"loggedIn": False}


def account_logout(config_dir: Any) -> None:
    _run_cli(config_dir, "logout")


def account_usage(config_dir: Any, **_kw: Any) -> list[dict[str, Any]]:
    """Plan windows of one account: a key's rate-limit headers, a sign-in's usage summary."""
    from .usage import account_session, fetch_usage

    env = account_env(config_dir)
    if ACCOUNT_DIR_ENV in env:
        session = account_session(config_dir)
        return fetch_usage("", session=session).get("windows") or [] if session else []
    return fetch_usage(env.get("CURSOR_API_KEY") or "", session="").get("windows") or []
