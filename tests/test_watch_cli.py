"""`mi watch` 的端到端接线（不连真 broker）。

用假 MQTT 客户端把消息推进来，验证设备解析、spec 翻译、输出格式、退出条件。
"""
import json
import time

import pytest
from typer.testing import CliRunner

from mi_home_cli.cli.app import app
from mi_home_cli.store import AuthData, Profile

SPEC_INSTANCE = {
    "type": "urn:miot-spec-v2:device:light:0000A001:x:1",
    "description": "Light",
    "services": [
        {
            "iid": 2,
            "type": "urn:miot-spec-v2:service:light:00007802:x:1",
            "properties": [
                {
                    "iid": 1,
                    "type": "urn:miot-spec-v2:property:on:00000006:x:1",
                    "description": "Switch Status",
                    "format": "bool",
                    "access": ["read", "write", "notify"],
                },
                {
                    "iid": 2,
                    "type": "urn:miot-spec-v2:property:brightness:0000000D:x:1",
                    "description": "Brightness",
                    "format": "uint8",
                    "access": ["read", "write", "notify"],
                    "unit": "percentage",
                    "value-range": [1, 100, 1],
                },
            ],
            "events": [
                {
                    "iid": 1,
                    "type": "urn:miot-spec-v2:event:low-battery:00000001:x:1",
                    "description": "Low Battery",
                }
            ],
        }
    ],
}

URN = SPEC_INSTANCE["type"]
MESSAGES = [
    ("device/d1/up/properties_changed/2/1", b'{"params":{"siid":2,"piid":1,"value":true}}'),
    ("device/d1/up/properties_changed/2/2", b'{"params":{"siid":2,"piid":2,"value":60}}'),
    ("device/d1/up/properties_changed/2/2", b'{"params":{"siid":2,"piid":2,"value":80}}'),
    ("device/d1/up/event_occured/2/1", b'{"params":{"siid":2,"eiid":1,"arguments":[]}}'),
    ("device/d1/state/online", b'{"device_id":"d1","event":"offline"}'),
]


class FakeClient:
    """连上就立刻把预置消息全推过来。"""

    def __init__(self, *args, **kwargs):
        self.subscribed = []

    def tls_set(self, **kwargs):
        pass

    def tls_set_context(self, context):
        pass

    def enable_logger(self, logger=None):
        pass

    def username_pw_set(self, username, password):
        pass

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def connect(self, *args):
        pass

    def loop_start(self):
        self.on_connect(self, None, None, None)
        for topic, payload in MESSAGES:
            message = type("M", (), {"topic": topic, "payload": payload})()
            self.on_message(self, None, message)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


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
            "homes": [{"home_id": "h1", "home_name": "我家", "rooms": []}],
            "devices": {
                "d1": {
                    "did": "d1", "name": "客厅灯", "model": "x.light.v1",
                    "urn": URN, "online": True, "home_id": "h1",
                    "home_name": "我家", "room_id": "r1", "room_name": "客厅",
                }
            },
        }
    )
    profile.spec_dir.mkdir(parents=True, exist_ok=True)
    # 预置 spec 缓存，测试不碰网络
    (profile.spec_dir / (URN.replace(":", "_") + ".json")).write_text(
        json.dumps(
            {
                "urn": URN,
                "fetched_at": int(time.time()),
                "instance": SPEC_INSTANCE,
                "translations": {},
            }
        ),
        encoding="utf-8",
    )
    import mi_home_cli.core.mqtt as module

    monkeypatch.setattr(module.mqtt, "Client", FakeClient)
    return tmp_path


def test_watch_streams_json_lines(env):
    result = CliRunner().invoke(
        app, ["-o", "json", "watch", "客厅灯", "--exit-after", "5"]
    )
    assert result.exit_code == 0, result.output
    lines = [
        json.loads(line)
        for line in result.output.splitlines()
        if line.startswith("{")
    ]
    assert len(lines) == 5
    assert lines[0] == {
        **lines[0],
        "kind": "prop",
        "property": "light.on",
        "value": True,
        "old": None,
        "did": "d1",
        "device": "客厅灯",
    }
    # 第三条是同一个属性的第二次变化，要带上一次的值
    assert (lines[2]["property"], lines[2]["old"], lines[2]["value"]) == (
        "light.brightness", 60, 80,
    )
    assert lines[3]["kind"] == "event" and lines[3]["event"] == "light.low-battery"
    assert lines[4] == {**lines[4], "kind": "state", "state": "offline"}


def test_watch_table_output_translates_names(env):
    result = CliRunner().invoke(app, ["watch", "客厅灯", "--exit-after", "3"])
    assert result.exit_code == 0, result.output
    assert "Switch Status" in result.output  # spec 里的说明
    assert "60% → 80%" in result.output  # 旧值 → 新值，带单位
    assert "共 3 条" in result.output


def test_watch_prop_filter(env):
    result = CliRunner().invoke(
        app, ["-o", "plain", "watch", "客厅灯", "--prop", "brightness", "--exit-after", "2"]
    )
    assert result.exit_code == 0, result.output
    assert "Brightness" in result.output
    assert "Switch Status" not in result.output


def test_watch_can_skip_events_and_state(env):
    result = CliRunner().invoke(
        app, ["watch", "客厅灯", "--no-events", "--no-state", "--exit-after", "3"]
    )
    assert result.exit_code == 0, result.output
    # 只订属性，不订事件和上下线
    assert "device/d1/up/event_occured/#" not in result.output


def test_watch_unknown_device(env):
    """错误本身要带着退出码，main() 照着它退出。"""
    from mi_home_cli.errors import DeviceNotFound

    result = CliRunner().invoke(app, ["watch", "不存在的灯", "--exit-after", "1"])
    assert isinstance(result.exception, DeviceNotFound)
    assert result.exception.exit_code == 3
