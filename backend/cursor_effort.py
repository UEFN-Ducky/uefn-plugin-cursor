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
