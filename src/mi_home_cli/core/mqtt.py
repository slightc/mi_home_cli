"""云端 MQTT：订阅设备的属性变化、事件和上下线。

连接参数取自 Home Assistant 米家集成的行为：
  broker   {region}-ha.mqtt.io.mi.com:8883（TLS，MQTT v5）
  用户名   OAuth 的 client_id
  密码     access_token
  客户端号 和 OAuth 的 device_id 一样，都是 ha.{32 位 hex}
"""
from __future__ import annotations

import json
import os
import queue
import ssl
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import paho.mqtt.client as mqtt

from ..errors import NetworkError
from . import const

BROKER_SUFFIX = "ha.mqtt.io.mi.com"
BROKER_PORT = 8883
KEEPALIVE = 60


CA_BUNDLE_ENV = "MI_CA_BUNDLE"


def _ssl_context() -> ssl.SSLContext:
    """TLS 上下文。

    顺序有讲究：
    1. MI_CA_BUNDLE 指定的证书优先——用自签 CA 做中间人的环境（企业代理、
       Clash/Surge 的 MITM）得靠它才能连上；
    2. 否则用系统默认（也会认 SSL_CERT_FILE），这样系统钥匙串里装了的
       企业根证书能直接生效；
    3. 系统里一张根证书都没有时（macOS 上的 Python 常见）再退回 certifi。
    强行只用 certifi 会把第 2 类用户挡在门外，所以 certifi 只做兜底。
    """
    override = os.environ.get(CA_BUNDLE_ENV)
    if override:
        return ssl.create_default_context(cafile=override)
    context = ssl.create_default_context()
    if not context.get_ca_certs():
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
    return context


def broker_host(region: str) -> str:
    return f"{region}-{BROKER_SUFFIX}"


def topic_props(did: str, siid: int | None = None, piid: int | None = None) -> str:
    tail = "#" if siid is None or piid is None else f"{siid}/{piid}"
    return f"device/{did}/up/properties_changed/{tail}"


def topic_events(did: str) -> str:
    # 上游把 occurred 拼错成 occured，服务端就是这么发的，只能跟着错
    return f"device/{did}/up/event_occured/#"


def topic_state(did: str) -> str:
    return f"device/{did}/state/#"


@dataclass
class Message:
    """一条已经解析好的推送。"""

    kind: str  # "prop" | "event" | "state"
    did: str
    siid: int | None = None
    iid: int | None = None  # piid 或 eiid
    value: Any = None
    arguments: list[Any] | None = None
    state: str | None = None
    raw: dict[str, Any] | None = None


def parse_message(topic: str, payload: bytes) -> Message | None:
    """把 MQTT 消息解析成 Message，解析不了返回 None。"""
    parts = topic.split("/")
    if len(parts) < 3 or parts[0] != "device":
        return None
    did = parts[1]
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None

    if "properties_changed" in topic:
        params = body.get("params")
        if not isinstance(params, dict) or "siid" not in params:
            return None
        return Message(
            kind="prop",
            did=did,
            siid=params.get("siid"),
            iid=params.get("piid"),
            value=params.get("value"),
            raw=body,
        )
    if "event_occured" in topic:
        params = body.get("params")
        if not isinstance(params, dict) or "siid" not in params:
            return None
        return Message(
            kind="event",
            did=did,
            siid=params.get("siid"),
            iid=params.get("eiid"),
            arguments=params.get("arguments") or [],
            raw=body,
        )
    if parts[2] == "state":
        event = body.get("event")
        if not event:
            return None
        return Message(
            kind="state", did=body.get("device_id", did), state=event, raw=body
        )
    return None


class CloudMqtt:
    """订阅端。消息丢进队列，由调用方在主线程里消费。"""

    def __init__(
        self,
        *,
        region: str,
        client_id: str,
        token_provider: Callable[[], str],
        on_state_change: Callable[[bool], None] | None = None,
        debug: bool = False,
    ) -> None:
        self.region = region
        self._token_provider = token_provider
        self._on_state_change = on_state_change
        self.messages: "queue.Queue[Message]" = queue.Queue()
        self._topics: list[str] = []
        self._connected = threading.Event()
        # 连不上时把真正的原因记下来：TCP 不通、TLS 握手失败、认证被拒，
        # 这三种问题排查方向完全不同，压成一句「超时」等于没说
        self._failure: str | None = None
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv5,
        )
        self._client.tls_set_context(_ssl_context())
        if debug:
            # paho 自己的日志能把 TLS 握手和 CONNACK 的细节打出来
            self._client.enable_logger()
        self._client.on_connect = self._handle_connect
        self._client.on_connect_fail = self._handle_connect_fail
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message

    # ---------- 回调 ----------

    def _handle_connect_fail(self, client, userdata=None):
        """TCP/TLS 阶段就没成——通常是端口不通或证书校验失败。"""
        self._failure = "连不上 broker（TCP 或 TLS 阶段失败）"

    def _handle_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            # 走到这说明 TCP/TLS 通了，是 broker 拒绝了连接
            self._failure = f"broker 拒绝连接：{reason_code}"
            self._connected.clear()
            return
        # 断线重连后要把订阅重新报一遍
        for topic in self._topics:
            client.subscribe(topic, qos=0)
        self._connected.set()
        if self._on_state_change:
            self._on_state_change(True)

    def _handle_disconnect(
        self, client, userdata, disconnect_flags=None, reason_code=None, properties=None
    ):
        self._connected.clear()
        if self._on_state_change:
            self._on_state_change(False)
        # access_token 可能已经续期，重连前把密码换成最新的
        try:
            client.username_pw_set(const.CLIENT_ID, self._token_provider())
        except Exception:
            pass

    def _handle_message(self, client, userdata, message):
        parsed = parse_message(message.topic, message.payload)
        if parsed is not None:
            self.messages.put(parsed)

    # ---------- 生命周期 ----------

    def subscribe(self, topics: Iterable[str]) -> None:
        for topic in topics:
            if topic not in self._topics:
                self._topics.append(topic)
        if self._connected.is_set():
            for topic in self._topics:
                self._client.subscribe(topic, qos=0)

    def start(self, timeout: float = 15.0) -> None:
        self._client.username_pw_set(const.CLIENT_ID, self._token_provider())
        host = broker_host(self.region)
        try:
            # 用同步 connect：异步版把握手异常闷在后台线程里，只剩一句超时，
            # 而证书校验失败这类问题必须把原文给用户看
            self._client.connect(host, BROKER_PORT, KEEPALIVE)
        except ssl.SSLCertVerificationError as err:
            raise NetworkError(
                f"{host}:{BROKER_PORT} 的证书校验失败：{err}",
                hint=(
                    "多半是代理在做中间人（Clash/Surge 的 fake-IP 会把域名解析到 "
                    "198.18.x.x）。给这个域名加一条直连规则，或用 "
                    f"{CA_BUNDLE_ENV} 指定代理的根证书"
                ),
            ) from err
        except (OSError, ssl.SSLError) as err:
            raise NetworkError(
                f"连接 {host}:{BROKER_PORT} 失败：{type(err).__name__}: {err}"
            ) from err
        self._client.loop_start()
        if not self._connected.wait(timeout):
            failure = self._failure
            self.stop()
            if failure:
                raise NetworkError(
                    f"连接 {broker_host(self.region)}:{BROKER_PORT} 失败：{failure}",
                    hint=(
                        "broker 拒绝连接多半是凭据问题，先跑 "
                        "`mi auth status --check`；"
                        "TCP/TLS 阶段失败则是网络封了 8883 端口"
                    ),
                )
            raise NetworkError(
                f"连接 {broker_host(self.region)}:{BROKER_PORT} 超时"
                f"（{timeout:.0f} 秒内没有任何回应）",
                hint=(
                    "多半是出口封了 8883 端口。用 `mi doctor` 看一眼，"
                    "或 `nc -vz " + broker_host(self.region) + " 8883` 直接测"
                ),
            )

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def __enter__(self) -> "CloudMqtt":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
