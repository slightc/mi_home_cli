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

    def tls_set_context(self, context):
        self.tls_context = context

    def enable_logger(self, logger=None):
        self.logger_enabled = True

    def username_pw_set(self, username, password):
        self.credentials.append((username, password))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def connect(self, host, port, keepalive):
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


def test_uses_certifi_ca_bundle():
    """paho 默认走系统 CA，macOS 上的 Python 常常找不到根证书——同一台机器

    httpx 能通、MQTT 连不上，多半就是这个。显式用 certifi 更稳。
    """
    import certifi

    from mi_home_cli.core.mqtt import _ssl_context

    context = _ssl_context()
    assert context.get_ca_certs(), "TLS 上下文里一张根证书都没有"
    assert certifi.where()  # 确认依赖装着


def test_tls_context_is_verifying():
    import ssl as _ssl

    from mi_home_cli.core.mqtt import _ssl_context

    context = _ssl_context()
    assert context.verify_mode == _ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_ca_bundle_env_overrides(monkeypatch, tmp_path):
    """中间人代理环境里得能指定代理自己的根证书，否则连不上。"""
    import certifi

    from mi_home_cli.core.mqtt import CA_BUNDLE_ENV, _ssl_context

    bundle = tmp_path / "ca.pem"
    bundle.write_text(open(certifi.where(), encoding="utf-8").read(), encoding="utf-8")
    monkeypatch.setenv(CA_BUNDLE_ENV, str(bundle))
    assert _ssl_context().get_ca_certs()


def test_system_store_is_tried_first_then_certifi(monkeypatch):
    """先系统后 certifi。

    不能只看「系统库是不是空的」来决定要不要退 certifi：库里有证书不代表有
    这条链需要的根，实测就有机器系统库非空却验不过小米 broker。所以两个都
    排进候选，按顺序试。
    """
    import certifi

    from mi_home_cli.core import mqtt as module

    monkeypatch.delenv(module.CA_BUNDLE_ENV, raising=False)
    contexts = module.ssl_contexts()
    assert len(contexts) == 2
    # 第二个才是 certifi 的
    assert certifi.where()


def test_explicit_bundle_is_the_only_candidate(monkeypatch, tmp_path):
    """用户显式指定了就只用它，别再偷偷回退——那会让「我指定的证书没生效」

    这种问题变得无从排查。
    """
    import certifi

    from mi_home_cli.core import mqtt as module

    bundle = tmp_path / "ca.pem"
    bundle.write_text(open(certifi.where(), encoding="utf-8").read(), encoding="utf-8")
    monkeypatch.setenv(module.CA_BUNDLE_ENV, str(bundle))
    assert len(module.ssl_contexts()) == 1


def test_new_client_per_ca_attempt(monkeypatch):
    """换证书库必须换客户端：paho 的 tls_set_context 只能调一次，

    第二次抛 ValueError('SSL/TLS has already been configured.')，
    在同一个客户端上重试等于白写。
    """
    import ssl as _ssl

    from mi_home_cli.core.mqtt import CloudMqtt

    built: list[FakeClient] = []

    class Tracking(FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.tls_calls = 0
            built.append(self)

        def tls_set_context(self, context):
            self.tls_calls += 1
            if self.tls_calls > 1:
                raise ValueError("SSL/TLS has already been configured.")

        def connect(self, host, port, keepalive):
            # 第一个客户端（系统 CA）验不过，第二个（certifi）成功
            if len(built) == 1:
                raise _ssl.SSLCertVerificationError("certificate verify failed")
            self.on_connect(self, None, None, None)

    import mi_home_cli.core.mqtt as module

    monkeypatch.setattr(module.mqtt, "Client", Tracking)
    notes: list[str] = []
    client = CloudMqtt(
        region="cn",
        client_id="ha.abc",
        token_provider=lambda: "AT",
        on_note=notes.append,
    )
    client.subscribe(["device/d1/state/#"])
    client.start(timeout=2)
    assert len(built) == 2, "第二次尝试没有新建客户端"
    assert all(item.tls_calls == 1 for item in built)
    assert "certifi" in notes[0]
    # 新客户端也要把订阅补上
    assert built[1].subscribed == ["device/d1/state/#"]
