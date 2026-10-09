"""Preflight and runner regressions; stub SDK success is not live readiness."""
from __future__ import annotations

import asyncio
import ast
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


@pytest.fixture
def native_launch(tmp_path, monkeypatch, launch):
    from backend.agent.coding_agents.proc_exec import ProcResult
    from frontend.ui_web import conversation_attachments
    node = shutil.which("node")
    assert node
    package = tmp_path / "node_modules/@cursor/sdk"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module","exports":"./index.js"}')
    (package / "index.js").write_text('''
export class AgentNotFoundError extends Error {}
const trace=(name,options)=>console.log(JSON.stringify({type:"trace",name,cwd:options?.local?.cwd}));
const agent={agentId:"new-session",close(){},async send(){trace("send");return {
 async *stream(){},async wait(){return process.env.CURSOR_FIX_CASE === "result_error"
 ? {status:"error",error:{message:"SYNTHETIC_SENSITIVE_MARKER",code:"ERROR"}}
 : {status:"finished",result:"legitimate answer"}}}}};
export const Agent={
 async create(options){trace("create",options);return agent},
 async resume(id,options){trace("resume",options);
  if(process.env.CURSOR_FIX_CASE==="missing")throw new AgentNotFoundError("SYNTHETIC_SENSITIVE_MARKER");
  if(process.env.CURSOR_FIX_CASE==="network")throw Error("expired not found SYNTHETIC_SENSITIVE_MARKER");
  return agent}
};
''', encoding="utf-8")
    (tmp_path / "runner.mjs").write_text(adapter._CURSOR_SDK_RUNNER, encoding="utf-8")
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps(VALID))
    monkeypatch.setattr(adapter, "which_cli", lambda *a: node)
    monkeypatch.setattr(adapter, "_cursor_sdk_sandbox", lambda: (tmp_path, False))
    monkeypatch.setattr(adapter, "_cached_model_row", lambda *a: {})
    monkeypatch.setattr(adapter, "coding_agents_tmp_dir", lambda: tmp_path)
    monkeypatch.setattr(conversation_attachments, "chat_attachments_dir", lambda *a, **k: None)
    records = []
    def process(**kw):
        proc = subprocess.run(kw["argv"], cwd=tmp_path, capture_output=True, text=True, timeout=8)
        records.append(proc)
        for line in proc.stdout.splitlines():
            kw["on_line"](line)
        return ProcResult(returncode=proc.returncode, stderr_tail=proc.stderr, raw_tail=proc.stdout)
    monkeypatch.setattr(adapter, "run_streaming_process", process)
    def run(session=""):
        return launch(str(config), session, public=True)
    run.records, run.events = records, launch.events
    return run


def test_missing_session_recovers_next_turn_real_core_storage(native_launch, monkeypatch, tmp_path):
    # Execute actual core storage functions and persistence branch without UI or
    # the real conversation store. The isolated JSON stands in for disk reload.
    source = ROOT.parents[1] / "UEFN-Ducky-Release/ducky_app/backend/agent/coding_agents/runner.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ("read_session_id", "store_session_id")]
    for function in functions:
        function.returns = None
        for arg in function.args.args:
            arg.annotation = None
    scope = {}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), scope)
    block = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "adapter.capabilities.resume")
    conv = types.SimpleNamespace(upstream_session_id="cursor:original-session", messages=["history"])
    monkeypatch.setenv("CURSOR_FIX_CASE", "missing")
    first = native_launch(scope["read_session_id"](conv, "cursor"))
    assert not first.ok and first.upstream_session_id == ""
    assert "next turn" in first.error.lower() and "workspace" in first.error.lower()
    scope.update(conv=conv, adapter=adapter.CursorAdapter(), result=first,
                 session_id="original-session", agent_id="cursor")
    exec(compile(ast.Module(body=[block], type_ignores=[]), str(source), "exec"), scope)
    persisted = tmp_path / "conversation.json"
    persisted.write_text(json.dumps(vars(conv)))
    conv = types.SimpleNamespace(**json.loads(persisted.read_text()))
    second = native_launch(scope["read_session_id"](conv, "cursor"))
    assert second.ok and second.upstream_session_id == "new-session"
    assert conv.messages == ["history"]
    traces = [[json.loads(line) for line in r.stdout.splitlines()
               if json.loads(line).get("type") == "trace"] for r in native_launch.records]
    assert [[t["name"] for t in turn] for turn in traces] == [["resume"], ["create", "send"]]
    assert traces[0][0]["cwd"] == traces[1][0]["cwd"] == str(tmp_path)


@pytest.mark.parametrize("session", ["", "original-session"])
def test_structured_result_error_private_at_all_boundaries(native_launch, monkeypatch, session):
    monkeypatch.setenv("CURSOR_FIX_CASE", "result_error")
    result = native_launch(session)
    assert not result.ok
    assert MARKER not in repr(result) + repr(native_launch.events)
    assert all(MARKER not in p.stdout + p.stderr for p in native_launch.records)


def test_network_not_found_words_do_not_clear_session(native_launch, monkeypatch):
    monkeypatch.setenv("CURSOR_FIX_CASE", "network")
    result = native_launch("original-session")
    assert not result.ok and result.upstream_session_id == "original-session"
    assert MARKER not in repr(result)


def test_missing_node_actionable_public_message(tmp_path, monkeypatch, launch):
    from frontend.ui_web import conversation_attachments
    monkeypatch.setattr(conversation_attachments, "chat_attachments_dir", lambda *a, **k: None)
    monkeypatch.setattr(adapter, "which_cli", lambda *a: None)
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(VALID))
    result = launch(str(path), "original-session", public=True)
    assert all(word in result.error.lower() for word in ("install", "node.js 22.13+", "npm", "path", "retry"))


@pytest.mark.parametrize("has_key,node,available", [(False, True, False), (True, False, False), (True, True, True)])
def test_detection_matches_supported_route(monkeypatch, launch, has_key, node, available):
    from backend.agent import secrets
    monkeypatch.setattr(secrets, "has_key", lambda *a: has_key)
    monkeypatch.setattr(adapter, "coding_agent_cfg", lambda *a: {})
    monkeypatch.setattr(adapter, "which_cli", lambda name, *a: "binary" if name in ("cursor-agent", "agent") or node else None)
    monkeypatch.setattr(adapter, "_known_models", lambda *a: [])
    info = adapter.CursorAdapter().detect(object())
    assert info.available is available
    assert "fallback" not in info.status.lower()
    if not has_key:
        assert "settings" in info.status.lower() and "api key" in info.status.lower()
    elif not node:
        assert "install" in info.status.lower()


@pytest.mark.parametrize("session", ["", "original-session"])
def test_older_app_registration_and_agent_launch(native_launch, monkeypatch, tmp_path, session):
    # Model the published base without the new mode helpers/constructor fields.
    # Import the actual plugin and use its registered factory, not a helper alone.
    import dataclasses
    from backend.agent.coding_agents import base
    legacy = types.ModuleType(base.__name__)
    removed = {"supported_modes", "requested_mode", "effective_mode"}
    for name in ("CodingAgentCapabilities", "CodingAgentInfo", "CodingAgentLaunchResult"):
        fields = []
        for field in dataclasses.fields(getattr(base, name)):
            if field.name in removed:
                continue
            if field.default_factory is not dataclasses.MISSING:
                default = dataclasses.field(default_factory=field.default_factory)
            elif field.default is not dataclasses.MISSING:
                default = dataclasses.field(default=field.default)
            else:
                default = dataclasses.field()
            fields.append((field.name, field.type, default))
        setattr(legacy, name, dataclasses.make_dataclass(name, fields))
    legacy.which_cli = adapter.which_cli
    monkeypatch.setitem(sys.modules, base.__name__, legacy)
    name = "_cursor_legacy_registration"
    spec = importlib.util.spec_from_file_location(name, ROOT / "backend/__init__.py",
                                                submodule_search_locations=[str(ROOT / "backend")])
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, plugin)
    # Keep update scheduling and graph registration inert in this isolated test.
    monkeypatch.setitem(sys.modules, name + ".cli_update", types.SimpleNamespace(
        schedule_cli_update_on_plugin_load=lambda: None))
    monkeypatch.setitem(sys.modules, name + ".graph_nodes", types.SimpleNamespace(register_nodes=lambda api: None))
    try:
        spec.loader.exec_module(plugin)
        registrations = {}
        api = types.SimpleNamespace(
            register_llm_provider=lambda *a, **k: None,
            register_coding_agent=lambda key, **kw: registrations.update(kw),
            register_ide_hookup=lambda *a, **k: None, log=lambda *a: None)
        plugin.register(api)
        instance = registrations["factory"]()
        loaded = sys.modules[name + ".cursor_adapter"]
        for attr in ("which_cli", "_cursor_sdk_sandbox", "_cached_model_row",
                     "coding_agents_tmp_dir", "run_streaming_process"):
            monkeypatch.setattr(loaded, attr, getattr(adapter, attr))
        assert not hasattr(legacy, "normalize_coding_mode")
        assert not hasattr(instance.capabilities, "supported_modes")
        result = instance.launch(prompt="test task", cwd=str(tmp_path), model="auto",
            mcp_config_path=str(tmp_path / "mcp.json"), conv_id="test", run_id="run",
            push=native_launch.events.append, session_id=session, system_prompt="",
            extra_args="", cli_path="", env={})
        assert result.ok and result.reply_text == "legitimate answer"
        assert isinstance(result, legacy.CodingAgentLaunchResult)
        assert not hasattr(result, "effective_mode")
        traces = [json.loads(line)["name"] for line in native_launch.records[-1].stdout.splitlines()
                  if json.loads(line).get("type") == "trace"]
        assert traces == (["resume", "send"] if session else ["create", "send"])
    finally:
        for key in list(sys.modules):
            if key.startswith(name + ".") and key not in (name + ".cli_update", name + ".graph_nodes"):
                del sys.modules[key]


@pytest.mark.parametrize("boundary", ["setup_none", "setup_raise", "process_raise", "sdk_missing"])
def test_setup_failure_public_recovery(native_launch, monkeypatch, boundary):
    from backend.agent.coding_agents.proc_exec import ProcResult
    def fail(*a, **kw):
        raise RuntimeError(MARKER)
    if boundary.startswith("setup"):
        monkeypatch.setattr(adapter, "_cursor_sdk_sandbox", fail if boundary == "setup_raise" else lambda: None)
    else:
        monkeypatch.setattr(adapter, "run_streaming_process", fail if boundary == "process_raise" else
                            lambda **kw: ProcResult(returncode=1, stderr_tail="SDK_UNAVAILABLE"))
    result = native_launch("original-session")
    assert not result.ok and result.upstream_session_id == "original-session"
    assert MARKER not in repr(result) + repr(native_launch.events)
    assert all(word in result.error.lower() for word in ("node.js 22.13+", "npm", "path", "retry"))


@pytest.mark.parametrize("code,diagnostic", [(1, adapter._SESSION_RESET), (3, "expired not found " + MARKER)])
def test_only_typed_runner_signal_clears_session(native_launch, monkeypatch, code, diagnostic):
    from backend.agent.coding_agents.proc_exec import ProcResult
    monkeypatch.setattr(adapter, "run_streaming_process", lambda **kw:
                        ProcResult(returncode=code, stderr_tail=diagnostic))
    result = native_launch("original-session")
    assert not result.ok and result.upstream_session_id == "original-session"
    assert MARKER not in repr(result)


@pytest.mark.parametrize("status", ["cancelled", "timeout"])
def test_structured_status_preserved_and_sanitized(native_launch, monkeypatch, status):
    from backend.agent.coding_agents.proc_exec import ProcResult
    def process(**kw):
        kw["on_line"](json.dumps({"type": "done", "status": status,
                                 "error": {"message": MARKER}}))
        return ProcResult(returncode=1, stderr_tail=MARKER)
    monkeypatch.setattr(adapter, "run_streaming_process", process)
    result = native_launch("original-session")
    assert not result.ok and result.status == status
    assert result.upstream_session_id == "original-session"
    assert MARKER not in repr(result) + repr(native_launch.events)
