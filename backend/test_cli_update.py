from __future__ import annotations

import os
from pathlib import Path

from cli_update import hidden_run_kwargs, npm_argv, plugin_package_version


def test_npm_argv_skips_cmd(tmp_path, monkeypatch):
    node = tmp_path / "node.exe"
    node.write_bytes(b"")
    cli = tmp_path / "node_modules" / "npm" / "bin" / "npm-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    npm = tmp_path / "npm.cmd"
    npm.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr("cli_update.shutil.which", lambda _name: None)
    argv = npm_argv(str(npm), ["install", "--omit=dev"])
    assert argv[:2] == [str(node), str(cli)]
    assert argv[2:] == ["install", "--omit=dev"]
    if os.name == "nt":
        kw = hidden_run_kwargs()
        assert kw.get("creationflags")
        assert kw.get("startupinfo") is not None


def test_plugin_wires_sdk_refresh():
    init = Path(__file__).with_name("__init__.py").read_text(encoding="utf-8")
    updater = Path(__file__).with_name("cli_update.py").read_text(encoding="utf-8")
    adapter = Path(__file__).with_name("cursor_adapter.py").read_text(encoding="utf-8")
    assert "schedule_cli_update_on_plugin_load" in init
    assert "update_sdk" in updater
    assert "_cursor_sdk_sandbox" in updater
    assert "npm_argv" in adapter
    assert plugin_package_version() == "1.0.33"


if __name__ == "__main__":
    test_plugin_wires_sdk_refresh()
    print("ok")
