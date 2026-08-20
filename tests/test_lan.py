"""miIO 局域网协议。

用一个自己实现的假设备（独立于被测代码的组包逻辑）跑完整链路：握手、加密、
校验、超时。真硬件上的验证只能在同网段做，这里保证协议层是对的。
"""
import hashlib
import json
import socket
import struct
import threading

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from mi_home_cli.core.lan import Endpoint, LanDevice, _parse_hello
from mi_home_cli.errors import DeviceOffline, MiCliError

TOKEN = "0123456789abcdef0123456789abcdef"
DID = 123456789
STAMP = 4242


def _cipher(token: bytes):
    key = hashlib.md5(token).digest()
    iv = hashlib.md5(key + token).digest()
    return Cipher(algorithms.AES128(key), modes.CBC(iv), default_backend())


class FakeDevice(threading.Thread):
    """会说 miIO 的假设备，组包逻辑照协议独立写一遍。"""

    def __init__(self, token: str = TOKEN, *, silent: bool = False, echo_id=None):
        super().__init__(daemon=True)
        self.token = bytes.fromhex(token)
        self.silent = silent
        self.echo_id = echo_id
        self.requests: list[dict] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(3)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()

    def endpoint(self) -> Endpoint:
        import time as _t

        return Endpoint(
            did=str(DID), ip="127.0.0.1", stamp=STAMP,
            offset=_t.time() - STAMP, port=self.port,
        )

    def run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            if self.silent:
                continue
            if data == b"\x21\x31\x00\x20" + b"\xff" * 28:
                self.sock.sendto(self._hello_reply(), addr)
                continue
            try:
                request = self._decrypt(data)
            except AssertionError:
                # 校验位对不上：真设备也是直接丢弃，不回任何东西
                continue
            self.requests.append(request)
            self.sock.sendto(self._reply(request), addr)

    def stop(self):
        self._stop.set()
        self.sock.close()

    def _hello_reply(self) -> bytes:
        return struct.pack(">HHQI16s", 0x2131, 32, DID, STAMP, b"\x00" * 16)

    def _decrypt(self, data: bytes) -> dict:
        total = struct.unpack(">H", data[2:4])[0]
        buffer = bytearray(data[:total])
        checksum = bytes(buffer[16:32])
        buffer[16:32] = self.token
        assert hashlib.md5(bytes(buffer)).digest() == checksum, "客户端算的校验位不对"
        decryptor = _cipher(self.token).decryptor()
        padded = decryptor.update(bytes(buffer[32:total])) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES128.block_size).unpadder()
        return json.loads(unpadder.update(padded) + unpadder.finalize())

    def _reply(self, request: dict) -> bytes:
        method = request.get("method")
        if method == "get_properties":
            result = [
                {**item, "value": 42, "code": 0} for item in request["params"]
            ]
        elif method == "set_properties":
            result = [
                {k: v for k, v in item.items() if k != "value"} | {"code": 0}
                for item in request["params"]
            ]
        elif method == "action":
            result = {"code": 0, "out": []}
        else:
            result = {"code": -1}
        body = {
            "id": self.echo_id if self.echo_id is not None else request["id"],
            "result": result,
        }
        clear = json.dumps(body).encode()
        padder = padding.PKCS7(algorithms.AES128.block_size).padder()
        encryptor = _cipher(self.token).encryptor()
        encrypted = encryptor.update(padder.update(clear) + padder.finalize())
        encrypted += encryptor.finalize()
        total = len(encrypted) + 32
        packet = bytearray(total)
        packet[:32] = struct.pack(">HHQI16s", 0x2131, total, DID, STAMP, self.token)
        packet[32:] = encrypted
        packet[16:32] = hashlib.md5(bytes(packet)).digest()
        return bytes(packet)


@pytest.fixture()
def device():
    fake = FakeDevice()
    fake.start()
    yield fake
    fake.stop()


def test_get_properties_roundtrip(device: FakeDevice):
    lan = LanDevice(str(DID), TOKEN, device.endpoint())
    result = lan.get_props([{"did": str(DID), "siid": 2, "piid": 1}])
    assert result == [{"did": str(DID), "siid": 2, "piid": 1, "value": 42, "code": 0}]
    assert device.requests[0]["method"] == "get_properties"


def test_set_properties_roundtrip(device: FakeDevice):
    lan = LanDevice(str(DID), TOKEN, device.endpoint())
    result = lan.set_props([{"did": str(DID), "siid": 2, "piid": 1, "value": True}])
    assert result[0]["code"] == 0
    assert device.requests[0]["params"][0]["value"] is True


def test_action_roundtrip(device: FakeDevice):
    lan = LanDevice(str(DID), TOKEN, device.endpoint())
    assert lan.call_action(str(DID), 2, 1, [5])["code"] == 0
    assert device.requests[0]["params"] == {
        "did": str(DID), "siid": 2, "aiid": 1, "in": [5],
    }


def test_request_carries_device_clock(device: FakeDevice):
    """请求里的时间戳要按设备自己的时钟推算，不是本机时间。"""
    lan = LanDevice(str(DID), TOKEN, device.endpoint())
    lan.get_props([{"did": str(DID), "siid": 2, "piid": 1}])
    packet = lan._build({"id": 1, "method": "x", "params": []})
    stamp = struct.unpack(">I", packet[12:16])[0]
    assert abs(stamp - STAMP) <= 2


def test_wrong_token_times_out_with_token_hint(device: FakeDevice):
    """token 不对时设备根本不回包，所以表现是超时——提示里必须提到 token，

    否则用户会一直去查网络。
    """
    lan = LanDevice(str(DID), "f" * 32, device.endpoint())
    with pytest.raises(DeviceOffline) as excinfo:
        lan.get_props([{"did": str(DID), "siid": 2, "piid": 1}])
    assert not device.requests  # 设备端确实丢弃了
    assert "token" in (excinfo.value.hint or "")


def test_timeout_reports_offline():
    fake = FakeDevice(silent=True)
    fake.start()
    try:
        lan = LanDevice(str(DID), TOKEN, fake.endpoint())
        with pytest.raises(DeviceOffline) as excinfo:
            lan.get_props(
                [{"did": str(DID), "siid": 2, "piid": 1}]
            )
        assert excinfo.value.exit_code == 5
    finally:
        fake.stop()


def test_stale_response_id_is_ignored():
    """上一次超时的迟到响应不能被当成这次的结果。"""
    fake = FakeDevice(echo_id=99999)
    fake.start()
    try:
        lan = LanDevice(str(DID), TOKEN, fake.endpoint())
        with pytest.raises(DeviceOffline):
            lan.get_props([{"did": str(DID), "siid": 2, "piid": 1}])
    finally:
        fake.stop()


def test_parse_hello():
    payload = struct.pack(">HHQI16s", 0x2131, 32, DID, STAMP, b"\xff" * 16)
    endpoint = _parse_hello(payload, 12.0, "192.168.1.9")
    assert endpoint.did == str(DID)
    assert endpoint.stamp == STAMP
    assert endpoint.ip == "192.168.1.9"
    assert _parse_hello(b"nope", 0, "1.2.3.4") is None


def test_bad_token_length_rejected_early():
    with pytest.raises(MiCliError):
        LanDevice("1", "abc", Endpoint("1", "127.0.0.1", 0, 0.0))


def test_hello_did_is_four_bytes_not_eight():
    """did 只占 4 字节，它前面的 unknown 字段回包里不一定是 0。

    实测有设备把 unknown 填成 16，按 8 字节读会得到 70044981670 这种
    离谱的 did，于是永远匹配不上云端清单、无法直连。
    """
    payload = (
        struct.pack(">HH", 0x2131, 32)
        + struct.pack(">I", 16)  # unknown ≠ 0
        + struct.pack(">I", 1325504934)  # did，仍在 32 位范围内
        + struct.pack(">I", STAMP)
        + b"\xff" * 16
    )
    endpoint = _parse_hello(payload, 0, "192.0.2.9")
    assert endpoint.did == "1325504934"


def test_broadcast_elapsed_is_not_labelled_as_rtt():
    """广播扫描测到的耗时包含排队等待，不能当成往返延迟。"""
    payload = struct.pack(">HHQI16s", 0x2131, 32, DID, STAMP, b"\xff" * 16)
    assert _parse_hello(payload, 654.0, "1.2.3.4").is_rtt is False
    assert _parse_hello(payload, 5.0, "1.2.3.4", is_rtt=True).is_rtt is True


def test_auto_channel_falls_back_when_lan_call_fails():
    """握手成功不代表设备愿意在局域网上干活——实测有设备对 get_properties

    回 user ack timeout。回落必须做在每次调用上，只在定位阶段回落不够。
    """
    from mi_home_cli.core.channel import AutoChannel
    from mi_home_cli.errors import MiCliError as _Err

    class FailingLan:
        name = "lan"

        def get_props(self, params):
            raise _Err("设备返回错误：user ack timeout")

        def set_props(self, params):
            raise _Err("超时")

        def call_action(self, did, siid, aiid, values):
            raise _Err("超时")

    class Cloud:
        name = "cloud"
        calls = 0

        def get_props(self, params):
            Cloud.calls += 1
            return [{"siid": 2, "piid": 1, "value": True, "code": 0}]

        def set_props(self, params):
            Cloud.calls += 1
            return [{"code": 0}]

        def call_action(self, did, siid, aiid, values):
            Cloud.calls += 1
            return {"code": 0}

    notes: list[str] = []
    channel = AutoChannel(FailingLan(), Cloud(), on_note=notes.append)
    assert channel.get_props([{"siid": 2, "piid": 1}])[0]["value"] is True
    assert "user ack timeout" in notes[0]
    # 失败过一次之后不再重试局域网，免得每条命令都白等
    channel.set_props([{"siid": 2, "piid": 1, "value": False}])
    channel.call_action("d", 2, 1, [])
    assert Cloud.calls == 3
    assert len(notes) == 1


def test_lan_failure_is_remembered_across_runs(tmp_path):
    """一次会话内不再重试还不够——下一条命令不该再白试一遍。"""
    import time as _t

    from mi_home_cli.core.channel import (
        clear_lan_failures,
        lan_failure,
        record_lan_failure,
    )
    from mi_home_cli.store import Profile

    profile = Profile("default", root=tmp_path)
    assert lan_failure(profile, "d1") is None

    record_lan_failure(profile, "d1", "user ack timeout")
    failed_at, reason = lan_failure(profile, "d1")
    assert reason == "user ack timeout"
    assert abs(failed_at - int(_t.time())) < 5

    # 重新扫描是新证据，标记要清掉（固件升级可能补上了支持）
    clear_lan_failures(profile)
    assert lan_failure(profile, "d1") is None


def test_lan_failure_expires(tmp_path, monkeypatch):
    from mi_home_cli.core import channel as channel_module
    from mi_home_cli.store import Profile

    profile = Profile("default", root=tmp_path)
    profile.write_lan({"d1": {"failed_at": 1, "reason": "旧的"}})
    assert channel_module.lan_failure(profile, "d1") is None
