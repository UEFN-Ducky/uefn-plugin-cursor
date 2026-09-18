"""Map the Ducky Effort dial onto Cursor.models.list parameter ids."""

from __future__ import annotations

from typing import Any

_EFFORT_PARAM_IDS = ("thinking", "effort", "reasoning", "reasoning_effort")
_LEVEL_RANK = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "extra-high": 5,
    "max": 6,
}


def pick_level(values: list[str], effort: str) -> str | None:
    """Nearest advertised level. off → none/minimal if listed, else omit."""
    wanted = (effort or "off").strip().lower()
    norm = {str(v).strip().lower(): str(v).strip() for v in values if str(v).strip()}
    if not norm:
        return None
    if wanted in ("", "off"):
        for cand in ("none", "minimal"):
            if cand in norm:
                return norm[cand]
        return None
    target = _LEVEL_RANK.get(wanted, 3)
    ranked = [(_LEVEL_RANK[k], k) for k in norm if k in _LEVEL_RANK]
    if not ranked:
        return None
    above = [k for r, k in ranked if r >= target]
    if above:
        above.sort(key=lambda k: _LEVEL_RANK[k])
        return norm[above[0]]
    ranked.sort()
    return norm[ranked[-1][1]]


def model_params(row: dict[str, Any] | None, effort: str) -> list[dict[str, str]]:
    """SDK ``model.params`` for the Effort dial. Only advertised ids."""
    params = (row or {}).get("params") if isinstance(row, dict) else None
    if not isinstance(params, dict):
        return []
    effort_n = (effort or "off").strip().lower()
    out: list[dict[str, str]] = []
    thinking_vals = {str(v).strip().lower() for v in (params.get("thinking") or [])}
    if thinking_vals:
        if effort_n in ("", "off"):
            if "false" in thinking_vals:
                out.append({"id": "thinking", "value": "false"})
        elif "true" in thinking_vals:
            out.append({"id": "thinking", "value": "true"})
    for pid in ("effort", "reasoning", "reasoning_effort"):
        vals = [str(v) for v in (params.get(pid) or [])]
        picked = pick_level(vals, effort_n)
        if picked is not None:
            out.append({"id": pid, "value": picked})
    return out


def row_supports_thinking_effort(row: dict[str, Any] | None) -> bool:
    params = (row or {}).get("params") if isinstance(row, dict) else None
    if not isinstance(params, dict):
        return False
    return any(params.get(k) for k in _EFFORT_PARAM_IDS)


def row_thinking_menu(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Menu stops from Cursor.models.list params. Off is always first."""
    if not row_supports_thinking_effort(row):
        return None
    params = (row or {}).get("params") if isinstance(row, dict) else {}
    if not isinstance(params, dict):
        return None
    seen: dict[str, str] = {}
    thinking_vals = {str(v).strip().lower() for v in (params.get("thinking") or [])}
    if "false" in thinking_vals or "none" in {str(v).strip().lower() for pid in ("effort", "reasoning", "reasoning_effort") for v in (params.get(pid) or [])}:
        seen["off"] = "off"
    for pid in ("effort", "reasoning", "reasoning_effort"):
        for raw in params.get(pid) or []:
            val = str(raw).strip().lower()
            if not val:
                continue
            if val in ("none", "minimal", "false"):
                seen["off"] = "off"
                continue
            seen[val] = val
    if "off" not in seen:
        seen["off"] = "off"
    order = ["off", "low", "medium", "high", "xhigh", "extra-high", "max"]
    levels: list[dict[str, Any]] = []
    for key in order:
        if key not in seen:
            continue
        levels.append(
            {
                "id": "off" if key == "off" else key,
                "label": "Off" if key == "off" else key.replace("-", " ").title(),
                "thinking_tokens": 0 if key == "off" else None,
                "hint": "No extended thinking" if key == "off" else f"{key}, no token cap",
            }
        )
    extra = [k for k in seen if k not in order]
    extra.sort()
    for key in extra:
        levels.append({"id": key, "label": key, "thinking_tokens": None, "hint": f"{key}, no token cap"})
    return {"lo": "Faster", "hi": "Smarter", "levels": levels}


def sdk_param_map(raw: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for pd in raw.get("parameters") or []:
        if not isinstance(pd, dict):
            continue
        pid = str(pd.get("id") or "").strip()
        if not pid:
            continue
        vals: list[str] = []
        for item in pd.get("values") or []:
            if isinstance(item, dict):
                val = str(item.get("value") or "").strip()
            else:
                val = str(item or "").strip()
            if val:
                vals.append(val)
        if vals:
            out[pid] = vals
    return out
