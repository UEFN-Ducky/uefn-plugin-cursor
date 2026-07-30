"""Cursor gateway — coding agent + API key row via host registries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_INSTALL_HELP = (
    "Needs the Cursor API key above (from cursor.com/dashboard/integrations — not your Cursor IDE sign-in). "
    "Node.js must be on PATH (Ducky installs @cursor/sdk automatically). Optional fallback: cursor-agent CLI."
)


def _test_key(api_key: str) -> dict[str, Any]:
    key = (api_key or "").strip()
    if not key:
        return {"ok": False, "detail": "No key"}
    if len(key) < 8:
        return {"ok": False, "detail": "Key looks too short"}
    return {"ok": True, "detail": "OK"}


def _complete_one_shot(*, model: str, system: str, user: str) -> str:
    from backend.agent.secrets import get_key, has_key

    from .cursor_adapter import CursorAdapter

    api_key = get_key("cursor")
    if not api_key or not has_key("cursor"):
        raise ValueError(
            "Add a Cursor API key under Settings → LLMs → Providers to use Cursor."
        )
    prompt = user if not system.strip() else f"{system}\n\n{user}"
    chunks: list[str] = []

    def push(ev: dict[str, Any]) -> None:
        if ev.get("type") == "text_delta" and ev.get("text"):
            chunks.append(str(ev["text"]))

    result = CursorAdapter()._try_node_sdk(
        prompt=prompt,
        cwd=str(Path.cwd()),
        model=model or "auto",
        mcp_config_path="",
        api_key=api_key,
        conv_id="plugin_llm",
        run_id="complete",
        push=push,
        session_id="",
        timeout_s=180.0,
    )
    if result is None:
        raise ValueError("Cursor SDK unavailable (need Node.js + @cursor/sdk).")
    if not result.ok:
        raise ValueError(result.error or "Cursor completion failed")
    text = (result.reply_text or "".join(chunks)).strip()
    if not text:
        raise ValueError("Cursor returned empty response")
    return text


def _normalize_model(model: str) -> str:
    mid = (model or "").strip()
    if not mid or mid.lower() == "default":
        return "auto"
    return mid


def _skills_dir() -> str:
    return str(Path.home() / ".cursor" / "skills")


def _fetch_models(api_key: str, **_kw: Any) -> Any:
    """Populate Settings → Default Model / catalog from Cursor.models.list (+ cache)."""
    from backend.agent.model_fetch import ModelInfo

    from .cursor_adapter import (
        _fetch_models_via_sdk,
        _known_models,
        _refresh_models_async,
        _write_models_cache,
    )

    key = (api_key or "").strip()
    if key:
        live = _fetch_models_via_sdk(key)
        if live:
            _write_models_cache(live)
        else:
            # Keep UI responsive — serve cache/fallback while SDK refresh runs.
            _refresh_models_async(key)

    out: list[ModelInfo] = []
    for row in _known_models():
        mid = str(row.get("id") or "").strip()
        if not mid:
            continue
        out.append(
            ModelInfo(
                id=mid,
                display_name=str(row.get("name") or mid).strip() or mid,
                supports_tools=True,
                supports_vision=True,
            )
        )
    return out


def _provider_factory(api_key: str, model: str, **_kw: Any) -> Any:
    """Cursor is a coding-agent gateway — embedded LLMProvider stream is unused."""

    class _CursorGatewayStub:
        def __init__(self) -> None:
            self.api_key = api_key
            self.model = model or "auto"

        async def stream_turn(self, **_kwargs: Any):
            raise RuntimeError(
                "Cursor runs as the Cursor coding agent — pick Cursor in the model picker "
                "(not the embedded Ducky agent)."
            )
            yield  # async-gen marker (unreachable)

        async def test_connection(self) -> tuple[bool, str]:
            res = _test_key(api_key)
            return bool(res.get("ok")), str(res.get("detail") or "")

    return _CursorGatewayStub()


def register(api) -> None:
    from .cursor_adapter import CursorAdapter

    # Models catalog (Default Model picker) — was missing; key alone never listed models.
    api.register_llm_provider(
        "cursor",
        factory=_provider_factory,
        fetch_models=_fetch_models,
        test_key=_test_key,
        key_optional=False,
    )
    api.register_coding_agent(
        "cursor",
        factory=lambda: CursorAdapter(),
        complete_one_shot=_complete_one_shot,
        test_key=_test_key,
        aliases=["cursor_agent", "cursor_sdk"],
        skills_dir=_skills_dir,
        normalize_model=_normalize_model,
        settings_defaults={"enabled": True, "cli_path": "", "default_args": ""},
        install_help=_INSTALL_HELP,
        token_provider="cursor",
        login_status_ok="api key saved",
    )
    api.register_ide_hookup("cursor", label="Cursor")
    api.log("Cursor gateway contribution active (Providers + Coding Agents + IDE + Models)")
