"""局域网直连（miIO 协议）。

UDP 54321，包结构：

    0-1    魔数 0x2131
    2-3    整包长度
    4-7    unknown（发包时填 0；但设备回包里不一定是 0，实测有设备填 16）
    8-11   did
    12-15  设备自己的时间戳
    16-31  md5 校验位（计算时这 16 字节先填 token）
    32-    AES-128-CBC 加密的 JSON-RPC，PKCS7 填充

密钥 = md5(token)，IV = md5(md5(token) + token)。握手（hello）包是 32 字节
全 ff，设备回一个同样格式的包，里面带它当前的时间戳——之后每个请求都要带上
按这个时间戳推算的值，否则设备会丢弃。

只能控直连路由器的 WiFi/有线设备；蓝牙 Mesh、ZigBee 那些走网关的设备不行。
"""
from __future__ import annotations

import hashlib
import json
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..errors import DeviceOffline, MiCliError, NetworkError

PORT = 54321
MAGIC = 0x2131
HEADER_LEN = 32
HELLO = bytes.fromhex("21310020" + "ffffffff" * 7)
BROADCAST = "255.255.255.255"


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()


@dataclass
class Endpoint:
    """一台设备在局域网上的位置和它的时钟。"""

    did: str
    ip: str
    stamp: int  # 设备自己报的时间戳
    offset: float  # 本机时间 - 设备时间戳，用来推算后续请求该填什么
    elapsed_ms: float = 0.0  # 从发出探测到收到这台设备应答的耗时
    port: int = PORT
    # 广播扫描时这个耗时包含了排队等待，不是往返延迟；只有单播探活
    # （ping）测出来的才是真延迟
    is_rtt: bool = False

    def current_stamp(self) -> int:
        return int(time.time() - self.offset)


def _parse_hello(
    data: bytes,
    elapsed_ms: float,
    ip: str,
    port: int = PORT,
    is_rtt: bool = False,
) -> Endpoint | None:
    if len(data) < HEADER_LEN or data[:2] != b"\x21\x31":
        return None
    # did 是 4 字节。它前面那 4 字节（unknown）在回包里不一定是 0——实测某设备
    # 填的是 16——所以不能把 8 个字节当成一个整数读，否则 did 会大出天际、
    # 永远匹配不上云端清单。
    did = struct.unpack(">I", data[8:12])[0]
    stamp = struct.unpack(">I", data[12:16])[0]
    return Endpoint(
        did=str(did),
        ip=ip,
        stamp=stamp,
        offset=time.time() - stamp,
        elapsed_ms=elapsed_ms,
        port=port,
        is_rtt=is_rtt,
    )


def discover(timeout: float = 3.0, address: str = BROADCAST) -> dict[str, Endpoint]:
    """广播 hello，收集应答。返回 did → Endpoint。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.3)
    found: dict[str, Endpoint] = {}
    try:
        started = time.time()
        try:
            sock.sendto(HELLO, (address, PORT))
        except OSError as err:
            raise NetworkError(f"广播失败：{err}") from err
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            endpoint = _parse_hello(data, (time.time() - started) * 1000, addr[0])
            if endpoint:
                found.setdefault(endpoint.did, endpoint)
    finally:
        sock.close()
    return found


def ping(ip: str, timeout: float = 0.6, port: int = PORT) -> Endpoint | None:
    """朝一个已知 IP 发 hello，用来确认缓存里的地址还有效。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        started = time.time()
        sock.sendto(HELLO, (ip, port))
        data, _ = sock.recvfrom(1024)
        return _parse_hello(
                data, (time.time() - started) * 1000, ip, port, is_rtt=True
            )
    except (socket.timeout, OSError):
        return None
    finally:
        sock.close()


class LanDevice:
    """一台可以直连的设备。"""

    def __init__(self, did: str, token: str, endpoint: Endpoint) -> None:
        if len(token) != 32:
            raise MiCliError(f"token 长度不对（应为 32 个十六进制字符）：{token!r}")
        self.did = did
        self.endpoint = endpoint
        self._token = bytes.fromhex(token)
        key = _md5(self._token)
        iv = _md5(key + self._token)
        self._cipher = Cipher(
            algorithms.AES128(key), modes.CBC(iv), default_backend()
        )
        self._msg_id = int(time.time()) % 10000

    # ---------- 组包 / 解包 ----------

    def _build(self, payload: dict[str, Any]) -> bytes:
        clear = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        padder = padding.PKCS7(algorithms.AES128.block_size).padder()
        encryptor = self._cipher.encryptor()
        body = encryptor.update(padder.update(clear) + padder.finalize())
        body += encryptor.finalize()
        total = len(body) + HEADER_LEN
        packet = bytearray(total)
        packet[:HEADER_LEN] = struct.pack(
            ">HHQI16s",
            MAGIC,
            total,
            int(self.did),
            self.endpoint.current_stamp(),
            self._token,
        )
        packet[HEADER_LEN:] = body
        # 校验位是「token 填在 [16:32] 时整包的 md5」
        packet[16:32] = _md5(bytes(packet))
        return bytes(packet)

    def _parse(self, data: bytes) -> dict[str, Any]:
        if len(data) < HEADER_LEN or data[:2] != b"\x21\x31":
            raise MiCliError("收到的不是 miIO 包")
        total = struct.unpack(">H", data[2:4])[0]
        buffer = bytearray(data[:total])
        checksum = bytes(buffer[16:32])
        buffer[16:32] = self._token
        if _md5(bytes(buffer)) != checksum:
            raise MiCliError(
                "校验失败，token 不对",
                hint="`mi device sync` 刷新一下设备清单，token 会随重新配网变化",
            )
        decryptor = self._cipher.decryptor()
        padded = decryptor.update(bytes(buffer[HEADER_LEN:total])) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES128.block_size).unpadder()
        clear = unpadder.update(padded) + unpadder.finalize()
        # 有些设备会在 JSON 末尾多塞一个 \0
        return json.loads(clear.rstrip(b"\x00"))

    # ---------- 调用 ----------

    def call(
        self, method: str, params: Any, *, timeout: float = 2.0, retries: int = 2
    ) -> Any:
        self._msg_id = (self._msg_id + 1) % 10000
        payload = {"id": self._msg_id, "method": method, "params": params}
        packet = self._build(payload)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            for attempt in range(retries):
                try:
                    sock.sendto(packet, (self.endpoint.ip, self.endpoint.port))
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError as err:
                    raise NetworkError(f"发往 {self.endpoint.ip} 失败：{err}") from err
                response = self._parse(data)
                if response.get("id") != payload["id"]:
                    # 上一次超时的迟到响应，丢掉继续等
                    continue
                if "error" in response:
                    error = response["error"]
                    raise MiCliError(
                        f"设备返回错误：{error.get('message', error)}"
                    )
                return response.get("result")
            raise DeviceOffline(
                f"{self.endpoint.ip} 在 {timeout * retries:.0f} 秒内没有响应",
                hint=(
                    "设备收不下这个包就不会回应答：token 不对（重新配网后会变，"
                    "跑 `mi device sync` 刷新）、设备刚离线、或换了 IP"
                    "（`mi lan discover` 重扫）"
                ),
            )
        finally:
            sock.close()

    # ---------- 和云端一致的接口 ----------

    def get_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = self.call(
            "get_properties",
            [
                {"did": self.did, "siid": item["siid"], "piid": item["piid"]}
                for item in params
            ],
        )
        return result if isinstance(result, list) else []

    def set_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = self.call(
            "set_properties",
            [
                {
                    "did": self.did,
                    "siid": item["siid"],
                    "piid": item["piid"],
                    "value": item["value"],
                }
                for item in params
            ],
        )
        return result if isinstance(result, list) else []

    def call_action(
        self, did: str, siid: int, aiid: int, values: list[Any]
    ) -> dict[str, Any]:
        result = self.call(
            "action", {"did": self.did, "siid": siid, "aiid": aiid, "in": values}
        )
        return result if isinstance(result, dict) else {"code": 0, "out": result}
