"""`mi home unset` / `mi home select` 的接线测试。"""
import time

import pytest
from typer.testing import CliRunner

from mi_home_cli.cli.app import app
from mi_home_cli.store import AuthData, Profile, read_config


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_HOME_CONFIG_DIR", str(tmp_path))
    profile = Profile("default", root=tmp_path)
    profile.write_auth(
        AuthData("AT", "RT", "cn", "ha.abc", int(time.time()), 10**9)
    )
    profile.write_devices(
        {
            "uid": "1",
            "synced_at": int(time.time()),
            "homes": [
                {"home_id": "h1", "home_name": "我家", "rooms": []},
                {"home_id": "h2", "home_name": "老家", "rooms": []},
            ],
            "devices": {
                "d1": {
                    "did": "d1", "name": "客厅灯", "model": "x.light.v1",
                    "urn": "", "online": True, "home_id": "h1",
                    "home_name": "我家", "room_id": "r1", "room_name": "客厅",
                }
            },
        }
    )
    return tmp_path


def _set_default(root):
    result = CliRunner().invoke(app, ["home", "use", "我家"])
    assert result.exit_code == 0, result.output


def test_unset_clears_default_home(env):
    _set_default(env)
    assert read_config(env).get("home", {}).get("id") == "h1"

    result = CliRunner().invoke(app, ["home", "unset"])
    assert result.exit_code == 0, result.output
    assert "已取消默认家庭" in result.output
    assert "home" not in read_config(env)


def test_unset_when_none_set(env):
    result = CliRunner().invoke(app, ["home", "unset"])
    assert result.exit_code == 0, result.output
    assert "当前没有设默认家庭" in result.output


def test_select_sets_default_home(env, monkeypatch):
    monkeypatch.setattr("mi_home_cli.render.is_tty", lambda: True)
    result = CliRunner().invoke(app, ["home", "select"], input="2\n")
    assert result.exit_code == 0, result.output
    assert "老家" in result.output
    assert read_config(env)["home"] == {"id": "h2", "name": "老家"}


def test_select_can_unset(env, monkeypatch):
    _set_default(env)
    monkeypatch.setattr("mi_home_cli.render.is_tty", lambda: True)
    result = CliRunner().invoke(app, ["home", "select"], input="0\n")
    assert result.exit_code == 0, result.output
    assert "已取消默认家庭" in result.output
    assert "home" not in read_config(env)


def test_select_marks_current_default(env, monkeypatch):
    _set_default(env)
    monkeypatch.setattr("mi_home_cli.render.is_tty", lambda: True)
    result = CliRunner().invoke(app, ["home", "select"], input="1\n")
    assert result.exit_code == 0, result.output
    assert "我家 *" in result.output


def test_select_rejects_out_of_range(env, monkeypatch):
    from mi_home_cli.errors import UsageError

    monkeypatch.setattr("mi_home_cli.render.is_tty", lambda: True)
    result = CliRunner().invoke(app, ["home", "select"], input="9\n")
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 2


def test_select_needs_tty(env, monkeypatch):
    from mi_home_cli.errors import UsageError

    monkeypatch.setattr("mi_home_cli.render.is_tty", lambda: False)
    result = CliRunner().invoke(app, ["home", "select"])
    assert isinstance(result.exception, UsageError)
    assert "交互式终端" in result.exception.message
