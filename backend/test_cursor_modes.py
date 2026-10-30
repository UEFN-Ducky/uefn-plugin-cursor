"""Mode routing and SDK compatibility without provider invocation."""
import ast
import dataclasses
import importlib.util
import itertools
import json
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
from backend.agent.coding_agents import base
from backend.agent.coding_agents.proc_exec import ProcResult


def load_adapter(monkeypatch, name="_cursor_modes"):
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT / "backend")]
    monkeypatch.setitem(sys.modules, name, package)
    spec = importlib.util.spec_from_file_location(name + ".cursor_adapter", ROOT / "backend/cursor_adapter.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launch(monkeypatch, tmp_path):
    a = load_adapter(monkeypatch)
    from backend.agent import secrets
    from frontend.ui_web import conversation_attachments
    monkeypatch.setattr(secrets, "get_key", lambda *args: "synthetic-key")
    monkeypatch.setattr(conversation_attachments, "chat_attachments_dir", lambda *args, **kw: None)
    monkeypatch.setattr(a, "which_cli", lambda *args: "mock-node")
    monkeypatch.setattr(a, "_cursor_sdk_sandbox", lambda: (tmp_path, False))
    monkeypatch.setattr(a, "_cached_model_row", lambda *args: {})
    monkeypatch.setattr(a, "coding_agents_tmp_dir", lambda: tmp_path)
    records, events = [], []
    stream = [{"type": "text_delta", "text": "legitimate reply"}, {"type": "done", "status": "finished"}]
    proc = ProcResult(returncode=0)
    def process(**kw):
        if "-p" in kw["argv"]:
            records.append(kw)
            kw["on_line"](json.dumps({"type":"result", "result":"legitimate reply", "session_id":
                kw["argv"][kw["argv"].index("--resume")+1] if "--resume" in kw["argv"] else "new-session"}))
            return invoke.proc
        cfg = json.loads(Path(kw["argv"][-1]).read_text())
        records.append(cfg)
        kw["on_line"](json.dumps({"type": "meta", "agentId": cfg["agentId"] or "new-session"}))
        for event in stream:
            kw["on_line"](event if isinstance(event, str) else json.dumps(event))
        return invoke.proc
    monkeypatch.setattr(a, "run_streaming_process", process)
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"uefn": {"command": "mock-bridge"}}}))
    def invoke(session="", **kwargs):
        args = dict(prompt="original", system_prompt="bootstrap", cwd=str(tmp_path), conv_id="test",
                    model="default", mcp_config_path=str(config), extra_args="", cli_path="", env={},
                    push=events.append, session_id=session, run_id="run")
        args.update(kwargs)
        return invoke.instance.launch(**args)
    invoke.a, invoke.instance = a, a.CursorAdapter()
    invoke.records, invoke.events, invoke.stream, invoke.proc = records, events, stream, proc
    invoke.process, invoke.config = process, config
    yield invoke
    for name in list(sys.modules):
        if name.startswith("_cursor_modes."):
            sys.modules.pop(name, None)


@pytest.mark.parametrize("session", ["", "prior-session"])
@pytest.mark.parametrize("mode,canonical", [(None,"agent"),("","agent"),("AGENT","agent"),(" Agent ","agent"),("ask","ask"),(" Plan ","plan"),(" ",""),(0,""),(False,""),([],""),("unknown","")])
def test_modes_and_metadata(launch, session, mode, canonical):
    result = launch(session, mode=mode)
    assert result.requested_mode == canonical
    assert result.ok is (canonical in ("agent", "ask", "plan"))
    assert result.effective_mode == (canonical if result.ok else "")
    assert result.upstream_session_id == (session or "new-session") if result.ok else result.upstream_session_id == session
    assert len(launch.records) == int(result.ok)


@pytest.mark.parametrize("mode", ["invalid", 7])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("session", ["", "prior-session"])
def test_refusal_precedes_every_side_effect(launch, monkeypatch, mode, cancelled, session):
    def forbidden(*args, **kwargs): raise AssertionError("side effect before refusal")
    from backend.agent import secrets
    from frontend.ui_web import conversation_attachments
    for name in ("_ducky_preflight", "_read_ducky_mcp_config", "which_cli", "_cursor_sdk_sandbox", "_cached_model_row", "coding_agents_tmp_dir", "run_streaming_process"):
        monkeypatch.setattr(launch.a, name, forbidden)
    monkeypatch.setattr(secrets, "get_key", forbidden)
    monkeypatch.setattr(conversation_attachments, "chat_attachments_dir", forbidden)
    monkeypatch.setattr(launch.a.tempfile, "mkstemp", forbidden)
    cancel = threading.Event()
    if cancelled: cancel.set()
    result = launch(session, mode=mode, cancel=cancel)
    assert not result.ok and result.status == "error"  # mode refusal wins
    assert result.upstream_session_id == session and result.effective_mode == ""


def test_core_refusal_and_six_turn_sequence(launch):
    session = ""
    for mode in ("agent", "ask", "plan", "agent", "plan", "ask"):
        result = launch(session, **base.coding_mode_launch_kwargs(launch.instance, mode))
        assert result.ok
        session = result.upstream_session_id
    assert [r["agentId"] for r in launch.records if "agentId" in r] == ["", "new-session"]
    assert getattr(launch.instance.capabilities, "supported_modes") == ("agent", "ask", "plan")


@pytest.mark.parametrize("boundary", ["config", "key", "node", "setup", "model", "helper", "process", "cancel", "timeout", "error", "reset"])
@pytest.mark.parametrize("session", ["", "prior-session"])
def test_all_returns_have_authoritative_modes(launch, monkeypatch, boundary, session):
    from backend.agent import secrets
    kwargs = {}
    def fail(*args, **kw): raise RuntimeError("PRIVATE_SYNTHETIC")
    if boundary == "config": launch.config.write_bytes(b"\xff\xfe\x80")
    elif boundary == "key": monkeypatch.setattr(secrets, "get_key", lambda *a: "")
    elif boundary == "node": monkeypatch.setattr(launch.a, "which_cli", lambda *a: None)
    elif boundary == "setup": monkeypatch.setattr(launch.a, "_cursor_sdk_sandbox", fail)
    elif boundary == "model": kwargs["model"] = ""
    elif boundary == "helper": monkeypatch.setattr(launch.instance, "_try_node_sdk", lambda **kw: None)
    elif boundary == "process": monkeypatch.setattr(launch.a, "run_streaming_process", fail)
    elif boundary == "cancel":
        kwargs["cancel"] = threading.Event(); kwargs["cancel"].set()
    elif boundary == "timeout": launch.proc = ProcResult(returncode=0, timed_out=True)
    elif boundary == "error": launch.proc = ProcResult(returncode=1)
    else: launch.proc = ProcResult(returncode=3, stderr_tail=launch.a._SESSION_RESET)
    result = launch(session, **kwargs)
    assert not result.ok
    assert (result.requested_mode, result.effective_mode) == ("agent", "")
    expected_session = "" if boundary == "reset" else session or ("new-session" if boundary in ("timeout", "error") else "")
    assert result.upstream_session_id == expected_session
    assert "PRIVATE_SYNTHETIC" not in repr(result) + repr(launch.events)
    assert not list(Path(launch.config.parent).glob("cursor-sdk-*.json"))


def test_spoofed_events_and_result_metadata(launch, monkeypatch):
    original = {"type": "text_delta", "text": "answer", "requested_mode": "plan", "effective_mode": "plan"}
    def helper(**kw):
        kw["push"](original)
        return launch.a.CodingAgentLaunchResult(ok=True, status="done", requested_mode="ask", effective_mode="ask")
    monkeypatch.setattr(launch.instance, "_try_node_sdk", helper)
    result = launch()
    assert (result.requested_mode, result.effective_mode) == ("agent", "agent")
    assert launch.events[-1] == dict(original, requested_mode="agent", effective_mode="")
    assert original["effective_mode"] == "plan"


@pytest.mark.parametrize("session", ["", "prior-session"])
def test_agent_configuration_and_stream_parity(launch, session):
    launch.stream.insert(0, "malformed not JSON")
    result = launch(session, image_paths=["synthetic-image.png"], extra_args="--mode plan", env={"DUCKY_REQUESTED_MODE":"ask"})
    assert result.ok and result.reply_text == "legitimate reply"
    cfg = launch.records[0]
    assert cfg["model"] == "auto" and cfg["agentId"] == session
    assert ("bootstrap" in cfg["prompt"]) is (not bool(session))
    assert "synthetic-image.png" in cfg["prompt"]
    assert cfg["mcpServers"] == {"uefn":{"command":"mock-bridge"}}
    assert not {"mode", "tools", "sandboxOptions", "customTools"} & cfg.keys()
    assert all(e["requested_mode"] == "agent" and e["effective_mode"] == "" for e in launch.events)


@pytest.fixture(scope="module")
def historical_classes():
    # Read actual published 1.2.361 definitions, not guessed legacy signatures.
    app = Path(base.__file__).resolve().parents[4]
    source = subprocess.check_output(["git", "-C", str(app), "show",
        "f2094f09307472a19fdd1edf762065649aa734dc:ducky_app/backend/agent/coding_agents/base.py"], text=True)
    names = {"CodingAgentCapabilities", "CodingAgentInfo", "CodingAgentLaunchResult"}
    body = [n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name in names]
    namespace = dict(dataclass=dataclasses.dataclass, field=dataclasses.field, Any=object)
    exec(compile(ast.Module(body=body, type_ignores=[]), "<published-1.2.361-base>", "exec"), namespace)
    return {name: namespace[name] for name in names}


@pytest.mark.parametrize("caps_new,result_new,helper", list(itertools.product([False, True], repeat=3)))
@pytest.mark.parametrize("session", ["", "prior-session"])
def test_actual_host_loader_registration_mixed_hosts(launch, monkeypatch, historical_classes, caps_new, result_new, helper, session):
    from backend.uefn_plugins import host
    legacy = types.ModuleType(base.__name__)
    legacy.CodingAgentCapabilities = base.CodingAgentCapabilities if caps_new else historical_classes["CodingAgentCapabilities"]
    legacy.CodingAgentLaunchResult = base.CodingAgentLaunchResult if result_new else historical_classes["CodingAgentLaunchResult"]
    legacy.CodingAgentInfo = historical_classes["CodingAgentInfo"]
    legacy.which_cli = launch.a.which_cli
    if helper: legacy.normalize_coding_mode = base.normalize_coding_mode
    monkeypatch.setitem(sys.modules, base.__name__, legacy)
    name = host.backend_module_name("cursor_modes_fixture")
    monkeypatch.setitem(sys.modules, name + ".cli_update", types.SimpleNamespace(schedule_cli_update_on_plugin_load=lambda: None))
    monkeypatch.setitem(sys.modules, name + ".graph_nodes", types.SimpleNamespace(register_nodes=lambda api: None))
    monkeypatch.setattr(host, "_CODING_AGENT_FACTORIES", {})
    api = host._PluginApi("cursor_modes_fixture")
    for method in ("register_llm_provider", "register_ide_hookup", "log"):
        monkeypatch.setattr(api, method, lambda *a, **kw: None)
    try:
        package = host._import_backend("cursor_modes_fixture", ROOT, "backend")
        package.register(api)
        instance = host.get_coding_agent_registration("cursor")["factory"]()
        module = sys.modules[name + ".cursor_adapter"]
        assert module.CodingAgentLaunchResult is legacy.CodingAgentLaunchResult
        for attr in ("which_cli", "_cursor_sdk_sandbox", "_cached_model_row", "coding_agents_tmp_dir", "run_streaming_process"):
            monkeypatch.setattr(module, attr, getattr(launch.a, attr))
        launch.instance = instance
        result = launch(session, mode="agent")
        assert type(result) is legacy.CodingAgentLaunchResult
        assert type(instance.capabilities) is legacy.CodingAgentCapabilities
        assert result.ok and result.reply_text == "legitimate reply"
        serialized = result.to_dict()
        assert serialized["upstream_session_id"] == (session or "new-session")
        if result_new:
            assert (serialized["requested_mode"], serialized["effective_mode"]) == ("agent", "agent")
        else:
            assert not hasattr(result, "requested_mode") and not hasattr(result, "effective_mode")
        assert getattr(instance.capabilities, "supported_modes", ("agent",)) == (("agent", "ask", "plan") if caps_new else ("agent",))
        for mode in ("ask", "plan"):
            if not caps_new:
                with pytest.raises(ValueError): base.coding_mode_launch_kwargs(instance, mode)
            result = launch(session, mode=mode)
            assert result.ok
        # Real preflight uses precisely the same concrete class as its isinstance check.
        refused = launch(session, mcp_config_path="")
        assert type(refused) is legacy.CodingAgentLaunchResult and not refused.ok
        assert len(launch.records) == 3
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


@pytest.mark.parametrize("field", ["requested_mode", "effective_mode"])
def test_partial_result_shapes(launch, monkeypatch, historical_classes, field):
    cls = dataclasses.make_dataclass("PartialResult", [(field, str, "")], bases=(historical_classes["CodingAgentLaunchResult"],))
    monkeypatch.setattr(launch.a, "CodingAgentLaunchResult", cls)
    for mode in ("agent", "ask"):
        result = launch("prior-session", mode=mode)
        assert type(result) is cls
        assert getattr(result, field) == (mode if field == "requested_mode" else mode if result.ok else "")


def test_kwargs_only_capabilities_are_conservative(monkeypatch):
    class Caps:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    monkeypatch.setattr(base, "CodingAgentCapabilities", Caps)
    a = load_adapter(monkeypatch, "_cursor_kwargs_caps")
    assert not hasattr(a.CursorAdapter.capabilities, "supported_modes")
    with pytest.raises(ValueError): base.coding_mode_launch_kwargs(a.CursorAdapter(), "ask")


@pytest.mark.parametrize("symbol", ["CodingAgentCapabilities", "CodingAgentLaunchResult", "CodingAgentInfo", "which_cli"])
def test_missing_mandatory_import_is_not_hidden(monkeypatch, symbol):
    monkeypatch.delattr(base, symbol)
    with pytest.raises(ImportError): load_adapter(monkeypatch, "_cursor_missing_required")


def test_unrelated_constructor_typeerror_propagates(launch, monkeypatch):
    class BrokenResult:
        def __init__(self, **kwargs): raise TypeError("unrelated constructor failure")
    monkeypatch.setattr(launch.a, "CodingAgentLaunchResult", BrokenResult)
    with pytest.raises(TypeError, match="unrelated constructor failure"):
        launch(mode="ask")


@pytest.mark.parametrize("cancelled,timed_out", [(True,False), (False,True), (True,True)])
@pytest.mark.parametrize("session", ["", "prior-session"])
def test_terminal_precedence_no_replay_and_metadata(launch, session, cancelled, timed_out):
    launch.stream.insert(0, {"type":"tool_call", "call_id":"synthetic-tool", "name":"ducky_get_tools", "status":"running", "args":{}})
    launch.proc = ProcResult(returncode=3, stderr_tail=launch.a._SESSION_RESET, cancelled=cancelled, timed_out=timed_out)
    result = launch(session)
    assert not result.ok and result.status == ("cancelled" if cancelled else "timeout")
    assert result.upstream_session_id == (session or "new-session")
    assert result.reply_text == "legitimate reply" and result.blocks
    assert (result.requested_mode, result.effective_mode) == ("agent", "")
    assert len(launch.records) == 1
    assert not list(launch.config.parent.glob("cursor-sdk-*.json"))


def test_typed_reset_then_next_turn_fresh_and_generic_error_retains(launch):
    launch.proc = ProcResult(returncode=3, stderr_tail=launch.a._SESSION_RESET)
    first = launch("prior-session")
    assert not first.ok and first.upstream_session_id == ""
    launch.proc = ProcResult(returncode=0)
    second = launch(first.upstream_session_id)
    assert second.ok and second.effective_mode == "agent"
    assert [r["agentId"] for r in launch.records] == ["prior-session", ""]
    launch.proc = ProcResult(returncode=1, stderr_tail="expired not found network")
    third = launch(second.upstream_session_id)
    assert not third.ok and third.upstream_session_id == second.upstream_session_id
    assert third.reply_text == "legitimate reply" and third.effective_mode == ""


def test_usage_and_blocks_preserved(launch):
    launch.stream[0:0] = [
        {"type":"tool_call", "call_id":"synthetic-tool", "name":"ducky_get_tools", "status":"running", "args":{}},
        {"type":"tool_call", "call_id":"synthetic-tool", "name":"ducky_get_tools", "status":"completed", "result":"synthetic catalog"},
    ]
    launch.stream[-1]["usage"] = {"input_tokens":12,"output_tokens":3,"model":"auto"}
    result = launch()
    assert result.ok and result.usage["input_tokens"] == 12 and result.usage["output_tokens"] == 3
    assert result.blocks and result.reply_text == "legitimate reply"
    assert (result.requested_mode, result.effective_mode) == ("agent", "agent")


@pytest.mark.parametrize("exception", [GeneratorExit(), SystemExit(), KeyboardInterrupt()])
def test_control_flow_unchanged(launch, monkeypatch, exception):
    def fail(*args, **kwargs): raise exception
    monkeypatch.setattr(launch.a, "run_streaming_process", fail)
    with pytest.raises(type(exception)) as caught: launch()
    assert caught.value is exception
    assert not list(launch.config.parent.glob("cursor-sdk-*.json"))


def test_capability_constructor_errors_not_masked(monkeypatch):
    class Broken:
        def __init__(self, *, supported_modes=(), **kwargs): raise TypeError("unrelated caps failure")
    monkeypatch.setattr(base, "CodingAgentCapabilities", Broken)
    with pytest.raises(TypeError, match="unrelated caps failure"):
        load_adapter(monkeypatch, "_cursor_broken_caps")



@pytest.mark.parametrize("mode", ["ask", "plan"])
def test_readonly_argv_and_bridge(launch, mode):
    result = launch(mode=mode, extra_args="--force --yolo -f --mode=agent")
    assert result.ok and result.effective_mode == mode
    call = launch.records[0]
    argv = call["argv"]
    assert f"--mode={mode}" in argv and "-p" in argv
    assert argv[argv.index("--output-format")+1] == "stream-json"
    assert not {"--force", "--yolo", "-f", "--mode=agent"} & set(argv)
    assert ("ducky_create_plan" in argv[-1]) == (mode == "plan")
    assert json.loads((Path(call["env_extra"]["CURSOR_CONFIG_DIR"]) / "mcp.json").read_text())["mcpServers"]["uefn"]["command"] == "mock-bridge"


@pytest.mark.parametrize("mode", ["ask", "plan"])
@pytest.mark.parametrize("mutation", ["edit", "delete", "add"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "exception"])
def test_file_change_always_fails(launch, monkeypatch, mode, mutation, outcome):
    target = launch.config.parent / "project.txt"
    target.write_text("before")
    def process(**kw):
        if mutation == "edit": target.write_text("changed content")
        elif mutation == "delete": target.unlink()
        else: (target.parent / "new.txt").write_text("new")
        if outcome == "exception": raise RuntimeError("private")
        launch.proc = ProcResult(returncode=int(outcome == "error"), cancelled=outcome == "cancel")
        return launch.process(**kw)
    monkeypatch.setattr(launch.a, "run_streaming_process", process)
    result = launch(mode=mode)
    assert not result.ok and result.status == "error" and result.effective_mode == ""
    assert f"Cursor changed files in {mode.title()} mode:" in result.error
    assert ("new.txt" if mutation == "add" else "project.txt") in result.error


def test_missing_cli_is_actionable(launch, monkeypatch):
    monkeypatch.setattr(launch.a, "which_cli", lambda *a: None)
    result = launch(mode="ask")
    assert not result.ok and "agent CLI" in result.error and not launch.records


@pytest.mark.parametrize("mode", ["agent", "ask", "plan"])
@pytest.mark.parametrize("session", ["", "prior-session"])
@pytest.mark.parametrize("failure", [
    {"type": "system", "subtype": "init", "mcp_servers": []},
    {"type": "meta", "mcp_servers": [{"name": "uefn", "status": "failed"}]},
    {"type": "error", "message": "MCP server uefn failed to connect PRIVATE_DETAIL"},
    {"type": "result", "is_error": True, "result": "MCP server uefn failed to connect PRIVATE_DETAIL"},
])
def test_reported_ducky_failure_is_sticky(launch, monkeypatch, mode, session, failure):
    def process(**kw):
        kw["on_line"](json.dumps(failure))
        return launch.process(**kw)
    monkeypatch.setattr(launch.a, "run_streaming_process", process)
    result = launch(session, mode=mode)
    assert not result.ok and result.error == "Ducky's tools didn't connect"
    assert not result.reply_text and not result.blocks
    assert "PRIVATE_DETAIL" not in json.dumps(result.to_dict()) + json.dumps(launch.events)


@pytest.mark.parametrize("mode", ["agent", "ask", "plan"])
def test_ducky_connection_failure_on_stderr(launch, mode):
    launch.proc = ProcResult(returncode=1, stderr_tail="uefn MCP server unavailable PRIVATE_DETAIL")
    result = launch(mode=mode)
    assert not result.ok and result.error == "Ducky's tools didn't connect"
    assert not result.reply_text


@pytest.mark.parametrize("session", ["", "prior-session"])
@pytest.mark.parametrize("mode", ["agent", "ask", "plan"])
def test_cold_server_then_first_cursor_tool(launch, monkeypatch, session, mode):
    from backend.agent.coding_agents import readiness
    from backend.bridge import shared_mcp
    now, tools = [0.0], []
    monkeypatch.setattr(readiness.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(readiness.time, "sleep", lambda n: now.__setitem__(0, now[0]+n))
    monkeypatch.setattr(shared_mcp, "start_daemon_from_app", lambda: {"ok": True})
    monkeypatch.setattr(shared_mcp, "_daemon_answers", lambda **kw: now[0] >= 8)
    def process(**kw):
        assert now[0] == 8
        if "-p" in kw["argv"]:
            assert "--approve-mcps" in kw["argv"]
            config = json.loads((Path(kw["env_extra"]["CURSOR_CONFIG_DIR"])/"mcp.json").read_text())
        else:
            config = json.loads(Path(kw["argv"][-1]).read_text())
        assert "uefn" in config["mcpServers"]
        # Simulated provider calls the configured server before its first answer.
        assert shared_mcp._daemon_answers()
        tools.append("ducky_get_plan")
        kw["on_line"](json.dumps({"type": "system", "subtype": "init", "mcp_servers": [{"name": "uefn", "status": "connected"}]}))
        return launch.process(**kw)
    monkeypatch.setattr(launch.a, "run_streaming_process", process)
    result = readiness.launch_with_ready_tools(types.SimpleNamespace(launch=lambda **kw: launch(session, mode=mode)))
    assert result.ok and tools == ["ducky_get_plan"]


def test_user_text_about_a_failure_is_not_connection_metadata(launch):
    launch.stream.insert(0, {"type": "text_delta", "text": "Explain why uefn MCP server failed yesterday"})
    assert launch().ok


@pytest.mark.parametrize("mode", ["ask", "plan", "agent"])
def test_project_mcp_servers_are_disabled_before_cli_runs(launch, monkeypatch, mode):
    # Epic's ModelContextProtocol.GenerateClientConfig writes .cursor/mcp.json with
    # unreal-mcp; the CLI's --approve-mcps would load it next to Ducky's server.
    project = launch.config.parent
    (project / ".cursor").mkdir()
    (project / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {
        "unreal-mcp": {"url": "http://127.0.0.1:8000/mcp"}, "uefn": {"command": "stale"}}}))
    calls = []
    monkeypatch.setattr(launch.a.subprocess, "run",
                        lambda argv, **kw: calls.append((argv, kw, len(launch.records))))
    assert launch(mode=mode).ok
    if mode == "agent":  # the SDK path loads no project settings (settingSources: [])
        assert calls == []
        return
    assert [argv[1:] for argv, _kw, _runs in calls] == [["mcp", "disable", "unreal-mcp"]]
    argv, kw, runs_before = calls[0]
    assert runs_before == 0
    assert kw["cwd"] == str(project)
    assert kw["env"]["CURSOR_CONFIG_DIR"] == launch.records[0]["env_extra"]["CURSOR_CONFIG_DIR"]


def test_project_mcp_servers_stop_at_the_home_folder(launch, monkeypatch, tmp_path):
    home = tmp_path / "home"
    game = home / "Games" / "MyGame"
    for folder, servers in ((home, {"personal": {}}), (home / "Games", {"helper": {}, "uefn": {}}),
                            (game, {"unreal-mcp": {}, "helper": {}})):
        (folder / ".cursor").mkdir(parents=True)
        (folder / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": servers}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert launch.a._project_mcp_servers(str(game)) == ["unreal-mcp", "helper"]


@pytest.mark.parametrize("mode", ["agent", "ask"])
def test_key_account_runs_with_its_key(launch, mode):
    assert launch(mode=mode, env={"CURSOR_API_KEY": "team-key"}).ok
    rec = launch.records[0]
    assert (rec["apiKey"] if mode == "agent" else rec["env_extra"]["CURSOR_API_KEY"]) == "team-key"


def test_default_account_keeps_saved_key(launch):
    assert launch(mode="ask").ok
    assert launch.records[0]["env_extra"]["CURSOR_API_KEY"] == "synthetic-key"


def test_login_account_signs_the_chat_folder_in(launch, tmp_path):
    login = tmp_path / "accounts" / "cursor" / "ab12"
    login.mkdir(parents=True)
    (login / "auth.json").write_text('{"token": "t"}')
    env = {launch.a.ACCOUNT_DIR_ENV: str(login)}
    assert launch(mode="ask", env=env).ok
    cli_env = launch.records[0]["env_extra"]
    assert cli_env["CURSOR_API_KEY"] == "" and launch.a.ACCOUNT_DIR_ENV not in cli_env
    chat = Path(cli_env["CURSOR_CONFIG_DIR"])
    assert (chat / "auth.json").read_text() == '{"token": "t"}' and (chat / "mcp.json").is_file()
    result = launch(mode="agent", env=env)
    assert not result.ok and "API key account" in result.error


def load_plugin(monkeypatch):
    name = "_cursor_plugin"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "backend/__init__.py", submodule_search_locations=[str(ROOT / "backend")])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_account_hooks(monkeypatch, tmp_path):
    from backend.agent import accounts
    p = load_plugin(monkeypatch)
    monkeypatch.setattr(accounts, "list_accounts", lambda _p: [
        {"id": "default", "kind": "key"}, {"id": "team", "kind": "key"}, {"id": "me", "kind": "login"}])
    monkeypatch.setattr(accounts, "account_key", lambda prov, aid: f"{prov}-{aid}-key")
    assert p._account_env(tmp_path / "team") == {"CURSOR_API_KEY": "cursor-team-key"}
    assert p._account_env(tmp_path / "me") == {"DUCKY_CURSOR_ACCOUNT_DIR": str(tmp_path / "me")}
    monkeypatch.setattr(sys.modules["_cursor_plugin.cursor_adapter"], "which_cli", lambda *a: "agent")
    assert p._account_login(tmp_path / "me") == {"argv": ["agent", "login"],
                                                 "env": {"CURSOR_CONFIG_DIR": str(tmp_path / "me")}}
    assert p._account_login(None)["env"] == {}
    assert p.parse_status("Logged in as ana@example.com") == {"loggedIn": True, "email": "ana@example.com"}
    assert p.parse_status("Not logged in") == {"loggedIn": False}


def test_limit_errors(monkeypatch):
    a = load_adapter(monkeypatch)
    assert a.is_limit_error("You've hit your usage limit for Composer")
    assert a.is_limit_error("HTTP 429 Too Many Requests")
    assert not a.is_limit_error("Cursor CLI run failed.")
