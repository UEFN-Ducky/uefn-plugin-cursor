"""Preflight and runner regressions; stub SDK success is not live readiness."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "UEFN-Ducky-Release" / "ducky_app"))
pkg = types.ModuleType("_cursor_readiness_test")
pkg.__path__ = [str(ROOT / "backend")]
sys.modules[pkg.__name__] = pkg
spec = importlib.util.spec_from_file_location(pkg.__name__ + ".cursor_adapter", ROOT / "backend/cursor_adapter.py")
assert spec and spec.loader
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "backend" or k.startswith("backend.")}
try:
    spec.loader.exec_module(adapter)
    app_modules = {k: v for k, v in sys.modules.items() if k == "backend" or k.startswith("backend.")}
finally:
    for k in list(sys.modules):
        if k == "backend" or k.startswith("backend."):
            del sys.modules[k]
    sys.modules.update(saved)

ERROR = "Ducky tools unavailable: invalid or unreadable MCP configuration. Check the Ducky MCP setup."
MARKER = "SYNTHETIC_SENSITIVE_MARKER"
VALID = {"mcpServers": {"uefn": {"command": "node", "args": ["bridge.js"]}}}


@pytest.fixture
def launch(tmp_path, monkeypatch):
    for k, v in app_modules.items():
        monkeypatch.setitem(sys.modules, k, v)
    def forbidden(*args, **kwargs):
        pytest.fail("resolution/setup/process reached before preflight passed")
    for name in ("which_cli", "_cursor_sdk_sandbox", "run_streaming_process", "run_cli_in_terminal"):
        monkeypatch.setattr(adapter, name, forbidden, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from backend.agent import secrets
    from frontend.ui_web import conversation_attachments
    monkeypatch.setattr(secrets, "get_key", lambda _: "synthetic-key")
    monkeypatch.setattr(conversation_attachments, "chat_attachments_dir", forbidden)
    events = []
    def run(path, session="", public=False, **kw):
        args = dict(prompt="test task", cwd=str(tmp_path), model="auto", mcp_config_path=path,
                    conv_id="test", run_id="run", push=events.append, session_id=session)
        args.update(kw)
        if public:
            return adapter.CursorAdapter().launch(**args, system_prompt="", extra_args="", cli_path="", env={})
        return adapter.CursorAdapter()._try_node_sdk(**args, api_key="synthetic-key")
    run.events = events
    return run


@pytest.mark.parametrize("session", ["", "previous-session"])
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("raw", [
    b"", b"{", b"\xff\xfe\x80", b"[]", b"null", b"{}",
    b'{"mcpServers":[]}', b'{"mcpServers":{}}', b'{"mcpServers":{"uefn":null}}',
    b'{"mcpServers":{"uefn":{}}}', b'{"mcpServers":{"uefn":{"command":7}}}',
    b'{"mcpServers":{"uefn":{"command":"node","args":7}}}',
    b'{"mcpServers":{"uefn":{"command":"node","args":[7]}}}',
    b'{"mcpServers":{"uefn":{"command":"node","env":[]}}}',
    b'{"mcpServers":{"uefn":{"type":"bad","command":"node"}}}',
    b'{"mcpServers":{"uefn":{"url":7}}}',
    b'{"mcpServers":{"uefn":{"type":"http","url":"https://"}}}',
    b'{"mcpServers":{"uefn":{"url":"file:///bad"}}}',
])
def test_bad_config(tmp_path, launch, session, public, raw):
    path = tmp_path / "mcp.json"
    path.write_bytes(raw)
    result = launch(str(path), session, public)
    assert (result.ok, result.status, result.error, result.upstream_session_id) == (False, "error", ERROR, session)
    assert launch.events == []


@pytest.mark.parametrize("session", ["", "previous-session"])
@pytest.mark.parametrize("kind", ["empty", "missing", "directory", "denied", "ordinary"])
def test_unreadable_config(tmp_path, monkeypatch, launch, session, kind):
    path = tmp_path / "mcp.json"
    if kind == "directory":
        path.mkdir()
    if kind in ("denied", "ordinary"):
        def fail(*a, **kw):
            raise (PermissionError if kind == "denied" else RuntimeError)(MARKER)
        monkeypatch.setattr(Path, "read_text", fail)
    result = launch("" if kind == "empty" else str(path), session)
    assert result.error == ERROR and result.upstream_session_id == session
    assert MARKER not in repr(result) + repr(launch.events)


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_control_flow(tmp_path, monkeypatch, launch, exception):
    def fail(*a, **kw):
        raise exception()
    monkeypatch.setattr(Path, "read_text", fail)
    with pytest.raises(exception):
        launch(str(tmp_path / "mcp.json"))


@pytest.mark.parametrize("session", ["", "previous-session"])
@pytest.mark.parametrize("during", [False, True])
def test_cancel_preflight(tmp_path, monkeypatch, launch, session, during):
    cancel = threading.Event()
    def read(*a, **kw):
        cancel.set()
        return json.dumps(VALID)
    if during:
        monkeypatch.setattr(Path, "read_text", read)
    else:
        cancel.set()
    result = launch(str(tmp_path / "mcp.json"), session, cancel=cancel)
    assert result.status == "cancelled" and not result.ok
    assert result.upstream_session_id == session


@pytest.mark.parametrize("session", ["", "previous-session"])
@pytest.mark.parametrize("server", [
    VALID["mcpServers"]["uefn"], {"type": "stdio", "command": "node", "env": {"KEY": "test"}},
    {"url": "https://example.invalid/mcp", "headers": {"X-Test": "test"}},
    {"type": "sse", "url": "http://example.invalid/sse"},
])
def test_valid_snapshot(tmp_path, monkeypatch, launch, session, server):
    from backend.agent.coding_agents.proc_exec import ProcResult
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"uefn": server}}), encoding="utf-8")
    monkeypatch.setattr(adapter, "which_cli", lambda *_: "node")
    monkeypatch.setattr(adapter, "_cursor_sdk_sandbox", lambda: (tmp_path, False))
    monkeypatch.setattr(adapter, "_cached_model_row", lambda *_: {})
    monkeypatch.setattr(adapter, "coding_agents_tmp_dir", lambda: tmp_path)
    seen = []
    def process(**kw):
        cfg = json.loads(Path(kw["argv"][-1]).read_text(encoding="utf-8"))
        seen.append(cfg)
        kw["on_line"](json.dumps({"type": "done", "status": "finished", "result": "answer", "agentId": "returned"}))
        return ProcResult(returncode=0)
    monkeypatch.setattr(adapter, "run_streaming_process", process)
    result = launch(str(path), session)
    assert result.ok and result.upstream_session_id == "returned"
    assert seen[0]["mcpServers"] == {"uefn": server}
    assert seen[0]["agentId"] == session and seen[0]["prompt"] == "test task"
    assert not list(tmp_path.glob("cursor-sdk-*.json"))


@pytest.mark.parametrize("session", ["", "previous-session"])
def test_no_key_rejects_terminal(tmp_path, monkeypatch, launch, session):
    from backend.agent import secrets
    monkeypatch.setattr(secrets, "get_key", lambda _: "")
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    result = launch(str(path), session, public=True)
    assert not result.ok and result.error.startswith("Ducky tools unavailable:")
    assert "API key" in result.error and result.upstream_session_id == session


@pytest.mark.parametrize("session", ["", "previous-session"])
def test_sdk_unavailable_no_fallback(tmp_path, monkeypatch, launch, session):
    from frontend.ui_web import conversation_attachments
    monkeypatch.setattr(conversation_attachments, "chat_attachments_dir", lambda *a, **kw: None)
    monkeypatch.setattr(adapter, "which_cli", lambda *_: None)
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    result = launch(str(path), session, public=True)
    assert not result.ok and result.upstream_session_id == session
    assert result.error.startswith("Ducky tools unavailable:")


@pytest.mark.parametrize("session", ["", "previous-session"])
def test_cancel_wins_over_sdk_unavailable(tmp_path, monkeypatch, launch, session):
    from backend.agent.coding_agents.proc_exec import ProcResult
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setattr(adapter, "which_cli", lambda *_: "node")
    monkeypatch.setattr(adapter, "_cursor_sdk_sandbox", lambda: (tmp_path, False))
    monkeypatch.setattr(adapter, "_cached_model_row", lambda *_: {})
    monkeypatch.setattr(adapter, "coding_agents_tmp_dir", lambda: tmp_path)
    monkeypatch.setattr(adapter, "run_streaming_process",
                        lambda **kw: ProcResult(returncode=2, cancelled=True, stderr_tail="SDK_UNAVAILABLE"))
    result = launch(str(path), session)
    assert result.status == "cancelled" and result.upstream_session_id == session


@pytest.mark.parametrize("session", ["", "previous-session"])
@pytest.mark.parametrize("boundary", ["setup", "process", "cancel_setup", "abort", "resource_error"])
def test_sdk_failure_boundaries(tmp_path, monkeypatch, launch, session, boundary):
    from backend.agent.coding_agents.proc_exec import ProcResult
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    cancel = threading.Event()
    monkeypatch.setattr(adapter, "which_cli", lambda *_: "node")
    monkeypatch.setattr(adapter, "_cached_model_row", lambda *_: {})
    monkeypatch.setattr(adapter, "coding_agents_tmp_dir", lambda: tmp_path)
    def setup():
        if boundary == "setup":
            raise RuntimeError(MARKER)
        if boundary == "cancel_setup":
            cancel.set()
        return tmp_path, False
    def process(**kw):
        if boundary == "process":
            raise RuntimeError(MARKER)
        if boundary == "cancel_setup":
            pytest.fail("process started after setup cancellation")
        return ProcResult(returncode=1, stderr_tail=(
            "CURSOR_CANCELLED" if boundary == "abort" else "MCP resource not found or expired"
        ))
    monkeypatch.setattr(adapter, "_cursor_sdk_sandbox", setup)
    monkeypatch.setattr(adapter, "run_streaming_process", process)
    result = launch(str(path), session, cancel=cancel)
    assert not result.ok and result.upstream_session_id == session
    assert result.status == ("cancelled" if boundary in ("abort", "cancel_setup") else "error")
    assert MARKER not in repr(result) + repr(launch.events)
    assert not list(tmp_path.glob("cursor-sdk-*.json"))


# Execute the real embedded runner against a local stub module, never the vendor
# SDK. This verifies ordering/no replay without a network call or npm install.
@pytest.mark.parametrize("case", ["fresh", "resumed", "not_found", "network", "unknown", "abort", "missing", "create_error"])
def test_runner_resume_contract(tmp_path, case):
    node = shutil.which("node")
    assert node, "Node is required to exercise the embedded runner"
    package = tmp_path / "node_modules" / "@cursor" / "sdk"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module","exports":"./index.js"}', encoding="utf-8")
    stub = """
export class AgentNotFoundError extends Error {}
function trace(name) { console.log(JSON.stringify({type:"trace", name})); }
const agent = {agentId:"returned", close(){}, async send(prompt, options) {
  trace("send");
  if (!options.mcpServers.uefn) throw Error("lost MCP");
  return {async *stream(){}, async wait(){return {status:"finished",result:"answer"}}};
}};
export const Agent = {
  async create(options) {
    trace("create");
    if (process.env.TEST_CURSOR_CASE === "create_error") throw Error("SYNTHETIC_SENSITIVE_MARKER");
    return agent;
  },
  async resume(id, options) {
    trace("resume");
    const c = process.env.TEST_CURSOR_CASE;
    if (c === "not_found") throw new AgentNotFoundError("SYNTHETIC_SENSITIVE_MARKER");
    if (c === "network" || c === "unknown" || c === "abort") {
      const e = Error("expired not found SYNTHETIC_SENSITIVE_MARKER");
      e.name = c === "abort" ? "AbortError" : "UnknownAgentError";
      throw e;
    }
    return agent;
  }
};
"""
    (package / "index.js").write_text(stub, encoding="utf-8")
    runner = tmp_path / "runner.mjs"
    runner.write_text(adapter._CURSOR_SDK_RUNNER, encoding="utf-8")
    cfg = dict(cmd="prompt", model="auto", apiKey="test", cwd=str(tmp_path), prompt="test",
               mcpServers={} if case == "missing" else VALID["mcpServers"],
               agentId="" if case in ("fresh", "create_error", "missing") else "previous")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")
    result = subprocess.run([node, str(runner), str(config)], cwd=tmp_path,
                            env={**os.environ, "TEST_CURSOR_CASE": case}, capture_output=True,
                            text=True, timeout=10)
    traces = [json.loads(line)["name"] for line in result.stdout.splitlines()
              if json.loads(line).get("type") == "trace"]
    if case in ("fresh", "resumed"):
        assert result.returncode == 0
        assert traces == (["create", "send"] if case == "fresh" else ["resume", "send"])
    else:
        expected = [] if case == "missing" else ["create"] if case == "create_error" else ["resume"]
        assert result.returncode != 0 and traces == expected
        assert MARKER not in result.stdout + result.stderr
        assert ("session not found" in result.stderr.lower()) == (case == "not_found")
        if case == "abort":
            assert "CURSOR_CANCELLED" in result.stderr
