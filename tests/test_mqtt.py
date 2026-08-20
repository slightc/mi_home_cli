"""云端 MQTT 的消息解析与客户端接线。

真正的连接需要访问 8883 端口，测试里不碰网络，用假 client 验证接线。
"""
import queue

import pytest

from mi_home_cli.core import const
from mi_home_cli.core.mqtt import (
    CloudMqtt,
    broker_host,
    parse_message,
    topic_events,
    topic_props,
    topic_state,
)


def test_broker_host_per_region():
    assert broker_host("cn") == "cn-ha.mqtt.io.mi.com"
    assert broker_host("sg") == "sg-ha.mqtt.io.mi.com"


def test_topic_builders():
    assert topic_props("d1") == "device/d1/up/properties_changed/#"
    assert topic_props("d1", 2, 1) == "device/d1/up/properties_changed/2/1"
    # 服务端就是把 occurred 拼成 occured 的，不能"顺手修正"
    assert topic_events("d1") == "device/d1/up/event_occured/#"
    assert topic_state("d1") == "device/d1/state/#"


def test_parse_property_change():
    msg = parse_message(
        "device/d1/up/properties_changed/2/1",
        b'{"params":{"siid":2,"piid":1,"value":true}}',
    )
    assert (msg.kind, msg.did, msg.siid, msg.iid, msg.value) == (
        "prop", "d1", 2, 1, True,
    )


def test_parse_event():
    msg = parse_message(
        "device/d1/up/event_occured/5/1",
        b'{"params":{"siid":5,"eiid":1,"arguments":[{"piid":1,"value":3}]}}',
    )
    assert msg.kind == "event" and msg.iid == 1
    assert msg.arguments == [{"piid": 1, "value": 3}]


def test_parse_state_uses_payload_device_id():
    msg = parse_message(
        "device/d1/state/online",
        b'{"device_id":"d1","event":"online","model":"x"}',
    )
    assert (msg.kind, msg.did, msg.state) == ("state", "d1", "online")


@pytest.mark.parametrize(
    "topic,payload",
    [
        ("device/d1/up/properties_changed/#", b"not json"),
        ("device/d1/up/properties_changed/#", b'{"params":{}}'),  # 缺 siid
        ("device/d1/state/x", b'{"device_id":"d1"}'),  # 缺 event
        ("other/d1/whatever", b"{}"),
        ("device/d1/up/properties_changed/#", b'"just a string"'),
    ],
)
def test_parse_rejects_garbage(topic, payload):
    assert parse_message(topic, payload) is None


class FakeClient:
    """够用的 paho 替身。"""

    def __init__(self, *args, **kwargs):
        self.subscribed: list[str] = []
        self.credentials: list[tuple[str, str]] = []
        self.connected = False
        self.loop_running = False

    def tls_set(self, **kwargs):
        self.tls = kwargs

    def username_pw_set(self, username, password):
        self.credentials.append((username, password))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def connect_async(self, host, port, keepalive):
        self.target = (host, port, keepalive)

    def loop_start(self):
        self.loop_running = True

    def loop_stop(self):
        self.loop_running = False

    def disconnect(self):
        self.connected = False


@pytest.fixture()
def fake(monkeypatch) -> CloudMqtt:
    import mi_home_cli.core.mqtt as module

    monkeypatch.setattr(module.mqtt, "Client", FakeClient)
    tokens = iter(["AT1", "AT2", "AT3"])
    return CloudMqtt(
        region="cn",
        client_id="ha.abc",
        token_provider=lambda: next(tokens),
    )


def test_connect_subscribes_all_topics(fake: CloudMqtt):
    fake.subscribe(["a", "b", "a"])  # 重复的只留一份
    client = fake._client
    fake._handle_connect(client, None, None, reason_code=None)
    assert client.subscribed == ["a", "b"]


def test_reconnect_resubscribes(fake: CloudMqtt):
    """断线重连后订阅要重新报，否则连上了也收不到消息。"""
    fake.subscribe(["a"])
    client = fake._client
    fake._handle_connect(client, None, None, reason_code=None)
    fake._handle_disconnect(client, None)
    fake._handle_connect(client, None, None, reason_code=None)
    assert client.subscribed == ["a", "a"]


def test_disconnect_refreshes_password(fake: CloudMqtt):
    """access_token 可能在长连接期间续期，重连要用新的。"""
    client = fake._client
    fake._handle_disconnect(client, None)
    assert client.credentials[-1] == (const.CLIENT_ID, "AT1")
    fake._handle_disconnect(client, None)
    assert client.credentials[-1] == (const.CLIENT_ID, "AT2")


def test_messages_land_in_queue(fake: CloudMqtt):
    class M:
        topic = "device/d1/up/properties_changed/2/1"
        payload = b'{"params":{"siid":2,"piid":1,"value":42}}'

    fake._handle_message(fake._client, None, M())
    assert fake.messages.get_nowait().value == 42

    class Bad:
        topic = "device/d1/up/properties_changed/2/1"
        payload = b"garbage"

    fake._handle_message(fake._client, None, Bad())
    with pytest.raises(queue.Empty):
        fake.messages.get_nowait()


def test_connect_failure_does_not_mark_connected(fake: CloudMqtt):
    class Failure:
        is_failure = True

    fake._handle_connect(fake._client, None, None, reason_code=Failure())
    assert not fake._connected.is_set()
    assert fake._client.subscribed == []
