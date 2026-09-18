"""Live Cursor plan windows from the dashboard usage-summary."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

_PAIRS = (
    ("x-ratelimit-remaining-requests", "x-ratelimit-limit-requests", "x-ratelimit-reset-requests", "requests"),
    ("x-ratelimit-remaining-tokens", "x-ratelimit-limit-tokens", "x-ratelimit-reset-tokens", "tokens"),
)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        n = float(str(v).strip().rstrip("s"))
    except (TypeError, ValueError):
        return None
    if n != n or n < 0:
        return None
    return n


def reset_after_text(seconds: Any) -> str:
    s = _num(seconds)
    if s is None:
        return ""
    n = int(s)
    if n <= 0:
        return ""
    d, rem = divmod(n, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts: list[str] = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h" if d else f"{h} hr")
    if m and d == 0:
        parts.append(f"{m} min")
    return f"Resets in {' '.join(parts)}" if parts else "Resets soon"


def reset_at_iso(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    import datetime as _dt

    try:
        dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()
    except ValueError:
        n = _num(raw)
        if n is None:
            return ""
        if n > 10_000_000_000:
            n = n / 1000.0
        try:
            dt = _dt.datetime.fromtimestamp(n).astimezone()
        except (OverflowError, OSError, ValueError):
            return ""
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"Resets {dt.strftime('%a')} {hour}:{dt.strftime('%M %p')}"


def _pct_row(wid: str, label: str, used_pct: float, *, reset: str = "") -> dict[str, Any]:
    used = max(0.0, min(100.0, used_pct))
    row: dict[str, Any] = {
        "id": wid,
        "label": label,
        "used": used,
        "limit": 100.0,
        "readout": f"{int(round(used))}%",
    }
    if reset:
        row["reset"] = reset
    return row


def _blob_pct(blob: dict[str, Any]) -> float | None:
    for k in ("used_percent", "usedPercent", "percentUsed", "utilization", "totalPercentUsed"):
        n = _num(blob.get(k))
        if n is not None:
            return n * 100.0 if n <= 1.5 else n
    used = _num(blob.get("used"))
    limit = _num(blob.get("limit"))
    if used is not None and limit and limit > 0:
        return max(0.0, min(100.0, used / limit * 100.0))
    return None


def windows_from_cursor(data: Any) -> list[dict[str, Any]]:
    """5-hour / weekly when the vendor sends them; else included-plan percent."""
    if not isinstance(data, dict):
        return []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        mapping = (
            ("five_hour", "hourly", "5-hour limit"),
            ("fiveHour", "hourly", "5-hour limit"),
            ("primary_window", "hourly", "5-hour limit"),
            ("weekly", "weekly", "Weekly · all models"),
            ("secondary_window", "weekly", "Weekly · all models"),
        )
        for key, wid, label in mapping:
            blob = node.get(key)
            if not isinstance(blob, dict) or wid in seen:
                continue
            pct = _blob_pct(blob)
            if pct is None:
                continue
            reset = reset_after_text(blob.get("reset_after_seconds") or blob.get("resetAfterSeconds"))
            if not reset:
                reset = reset_at_iso(blob.get("reset_at") or blob.get("resetsAt") or blob.get("reset"))
            found.append(_pct_row(wid, label, pct, reset=reset))
            seen.add(wid)
        for v in node.values():
            if isinstance(v, (dict, list)):
                walk(v)

    walk(data)
    if found:
        return found
    plan = ((data.get("individualUsage") or {}) if isinstance(data.get("individualUsage"), dict) else {}).get("plan")
    if not isinstance(plan, dict):
        plan = {}
    auto = _num(plan.get("autoPercentUsed"))
    api = _num(plan.get("apiPercentUsed"))
    total = _num(plan.get("totalPercentUsed"))
    reset = reset_at_iso(data.get("billingCycleEnd"))
    out: list[dict[str, Any]] = []
    if auto is not None:
        out.append(_pct_row("included", "Included usage", auto, reset=reset))
    if api is not None:
        out.append(_pct_row("api", "API usage", api, reset=reset))
    if not out and total is not None:
        out.append(_pct_row("plan", "Plan usage", total, reset=reset))
    return out


def windows_from_headers(headers: Any) -> list[dict[str, Any]]:
    raw: dict[str, str] = {}
    items = headers.items() if headers is not None and hasattr(headers, "items") else []
    for k, v in items:
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        raw[str(k or "").lower()] = str(v or "").strip()
    out: list[dict[str, Any]] = []
    for rem_k, lim_k, reset_k, unit in _PAIRS:
        rem = _num(raw.get(rem_k))
        lim = _num(raw.get(lim_k))
        if rem is None or lim is None or lim <= 0:
            continue
        used = max(0.0, lim - rem)
        out.append({"id": unit, "label": unit[:1].upper() + unit[1:], "used": used, "limit": lim, "unit": unit})
    return out


def _cursor_session() -> str:
    env = (os.environ.get("CURSOR_SESSION_TOKEN") or "").strip()
    if env:
        return env
    appdata = os.environ.get("APPDATA") or ""
    db = Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not db.is_file():
        return ""
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key IN ('cursorAuth/accessToken','cursorAuth/cachedSignUpType') ORDER BY key LIMIT 1"
            ).fetchone()
            token = str(row[0] if row else "").strip()
            if token and not token.startswith("{"):
                return token
            row = con.execute("SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'").fetchone()
            return str(row[0] if row else "").strip()
        finally:
            con.close()
    except Exception:
        return ""


def fetch_usage(api_key: str, *, model: str = "") -> dict[str, Any]:
    session = _cursor_session()
    if session:
        try:
            import httpx

            r = httpx.get(
                "https://cursor.com/api/usage-summary",
                headers={"Cookie": f"WorkosCursorSessionToken={session}"},
                timeout=8.0,
                follow_redirects=True,
            )
            if r.status_code < 400:
                rows = windows_from_cursor(r.json())
                if rows:
                    return {"windows": rows}
        except Exception:
            pass
    key = (api_key or "").strip()
    if not key:
        return {"windows": []}
    try:
        import httpx

        r = httpx.get(
            "https://api.cursor.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=8.0,
            follow_redirects=True,
        )
        return {"windows": windows_from_headers(r.headers)}
    except Exception:
        return {"windows": []}
