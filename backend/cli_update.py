"""Refresh @cursor/sdk on Cursor plugin Store update for Ducky-only users."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

_update_lock = threading.Lock()
_busy = False
_last: dict[str, Any] = {}


def plugin_package_version() -> str:
    path = Path(__file__).resolve().parent.parent / "plugin.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("version") or "")
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def stamp_path() -> Path:
    from frontend.settings import default_app_data_dir

    return default_app_data_dir() / "coding_agents" / "cursor_sdk_plugin.json"


def sdk_installed_stamp() -> Path:
    from frontend.settings import default_app_data_dir

    return default_app_data_dir() / "coding_agents" / "cursor_sdk" / ".installed"


def read_stamp() -> dict[str, Any]:
    try:
        data = json.loads(stamp_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_stamp(data: dict[str, Any]) -> None:
    path = stamp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def status_text() -> str:
    if _busy:
        return "Updating Cursor SDK…"
    return str(_last.get("message") or "")


def hidden_run_kwargs() -> dict[str, Any]:
    """No console. npm.cmd still flashes — call npm through npm_argv()."""
    if os.name != "nt":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": info,
    }


def npm_argv(npm: str, args: list[str]) -> list[str]:
    """node.exe + npm-cli.js. npm.cmd is a batch file and opens a console."""
    bases: list[Path] = [Path(npm).parent]
    found = shutil.which("node")
    if found:
        bases.append(Path(found).parent)
    seen: set[str] = set()
    for base in bases:
        key = str(base).lower()
        if key in seen:
            continue
        seen.add(key)
        node = base / "node.exe"
        if not node.is_file():
            alt = base / "node"
            node = alt if alt.is_file() else node
        cli = base / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if node.is_file() and cli.is_file():
            return [str(node), str(cli), *args]
    return [npm, *args]


def needs_plugin_load_update() -> bool:
    stamp = read_stamp()
    if not stamp.get("ok"):
        return True
    return str(stamp.get("plugin_version") or "") != plugin_package_version()


def update_sdk() -> dict[str, Any]:
    """Pull @cursor/sdk@latest. A failed npm install keeps the copy already on disk."""
    global _busy, _last
    with _update_lock:
        _busy = True
        try:
            from .cursor_adapter import _cursor_sdk_sandbox

            root, installed = _cursor_sdk_sandbox(force=True)
            if installed and root is not None:
                write_stamp(
                    {
                        "plugin_version": plugin_package_version(),
                        "updated_at": time.time(),
                        "ok": True,
                    }
                )
                result = {"ok": True, "message": "Cursor SDK updated", "error": ""}
            elif root is not None:
                result = {
                    "ok": False,
                    "message": "Cursor SDK update failed",
                    "error": "npm install failed — using the SDK already on disk",
                }
            else:
                result = {
                    "ok": False,
                    "message": "Cursor SDK update failed",
                    "error": "Node.js / npm required to install @cursor/sdk",
                }
            _last = result
            return result
        finally:
            _busy = False


def schedule_cli_update_on_plugin_load() -> None:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("DUCKY_SKIP_CLI_UPDATE"):
        return
    try:
        if not needs_plugin_load_update():
            return
    except Exception:
        return

    def _worker() -> None:
        try:
            update_sdk()
        except Exception:
            pass

    threading.Thread(target=_worker, name="cursor-sdk-update", daemon=True).start()
