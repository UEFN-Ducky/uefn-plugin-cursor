"""Cursor one-click sign-in: Settings' Log in installs the agent CLI when missing, then a
hidden `agent login` opens Cursor's sign-in in the browser and waits for it. The accounts
row polls account_status until it reads signed in. No terminal tab."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

INSTALL_PS = "irm 'https://cursor.com/install?win32=true' | iex"
LOGIN_TIMEOUT_S = 300.0
WAITING = "Sign in to Cursor in the browser tab that just opened…"
NOT_FINISHED = "Sign-in didn't finish, try again."
INSTALLING = "The Cursor agent CLI isn't installed yet. Ducky is installing it; sign-in starts by itself."

# ponytail: one hidden sign-in per config folder in this process; a second Log in replaces it.
_LOCK = threading.Lock()
_STATE: dict[str, dict[str, Any]] = {}  # normcased folder ("" = the CLI's own) -> {proc, pending, error, out}
_INSTALL: dict[str, Any] = {"busy": False, "error": ""}


def _hidden() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0), "startupinfo": info}


def _install_argv() -> list[str]:
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        ps = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        return [str(ps) if ps.is_file() else "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-Command", INSTALL_PS]
    return ["sh", "-c", "curl https://cursor.com/install -fsS | bash"]


def install_in_background(find_cli: Callable[[], str]) -> None:
    def work() -> None:
        try:
            proc = subprocess.run(_install_argv(), capture_output=True, text=True, timeout=600,
                                  encoding="utf-8", errors="replace", **_hidden())
            ok = proc.returncode == 0 and bool(find_cli())
            _INSTALL["error"] = "" if ok else f"Cursor CLI install failed: {(proc.stderr or proc.stdout or '').strip()[-300:]}"
        except Exception as exc:  # never leave the row spinning
            _INSTALL["error"] = f"Cursor CLI install failed: {exc}"
        finally:
            _INSTALL["busy"] = False

    if _INSTALL["busy"]:
        return
    _INSTALL.update(busy=True, error="")  # before the thread: the row's first poll sees it
    threading.Thread(target=work, name="cursor-cli-install", daemon=True).start()


def _key(config_dir: Any) -> str:
    return os.path.normcase(str(config_dir)) if config_dir else ""


def _stop(proc: Any) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def start_browser_login(config_dir: Any, cli: str, env: dict[str, str], *,
                        popen: Callable[..., Any] = subprocess.Popen,
                        timeout_s: float = LOGIN_TIMEOUT_S, poll_s: float = 0.5) -> dict[str, Any]:
    if config_dir:
        Path(config_dir).mkdir(parents=True, exist_ok=True)
    key = _key(config_dir)
    with _LOCK:
        old = _STATE.pop(key, None)
    if old:
        _stop(old["proc"])
    try:
        proc = popen([cli, "login"], env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                     encoding="utf-8", errors="replace", **_hidden())
    except OSError as exc:
        return {"error": f"Could not start the Cursor agent CLI: {exc}"}
    out: list[str] = []
    if getattr(proc, "stdout", None) is not None:  # drained, or a full pipe stalls the login
        threading.Thread(target=lambda: out.extend(proc.stdout), name="cursor-login-out", daemon=True).start()
    state = {"proc": proc, "pending": True, "error": "", "out": out}
    with _LOCK:
        _STATE[key] = state
    threading.Thread(target=_watch, args=(state, timeout_s, poll_s), name="cursor-login", daemon=True).start()
    return {"message": WAITING}


def _watch(state: dict[str, Any], timeout_s: float, poll_s: float) -> None:
    proc = state["proc"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(poll_s)
    _stop(proc)  # timed out
    error = ""
    if getattr(proc, "returncode", 1) != 0:
        lines = [ln.strip() for ln in "".join(state["out"]).splitlines() if "error" in ln.lower()]
        error = f"{NOT_FINISHED} {lines[-1][:200]}" if lines else NOT_FINISHED
    with _LOCK:
        state.update(pending=False, error=error)


def login_state(config_dir: Any) -> dict[str, Any]:
    """{installing+pending} · {pending} · {error} · {} for account_status to merge."""
    if _INSTALL["busy"]:
        return {"pending": True, "installing": True}
    with _LOCK:
        st = dict(_STATE.get(_key(config_dir)) or {})
    if st.get("pending"):
        return {"pending": True}
    err = st.get("error") or _INSTALL["error"]
    return {"error": err} if err else {}
