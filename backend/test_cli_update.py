from __future__ import annotations

from pathlib import Path

from cli_update import plugin_package_version


def test_plugin_wires_sdk_refresh():
    init = Path(__file__).with_name("__init__.py").read_text(encoding="utf-8")
    updater = Path(__file__).with_name("cli_update.py").read_text(encoding="utf-8")
    assert "schedule_cli_update_on_plugin_load" in init
    assert "update_sdk" in updater
    assert "_cursor_sdk_sandbox" in updater
    assert plugin_package_version() == "1.0.22"


if __name__ == "__main__":
    test_plugin_wires_sdk_refresh()
    print("ok")
