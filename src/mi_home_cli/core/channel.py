"""控制通道：云端 / 局域网 / 自动。

上层命令不关心走的是哪条路，只调 get_props / set_props / call_action。
"""
from __future__ import annotations

import time
from typing import Any, Protocol

from ..errors import DeviceOffline, MiCliError
from ..store import Profile
from .cloud import CloudApi
from .lan import Endpoint, LanDevice, discover, ping
from .registry import Device
from .session import Session

# 只有直连路由器的设备能走局域网；蓝牙 Mesh / ZigBee 那些经网关接入的不行。
# 取值来自上游对 connect_type（云端的 pid 字段）的判断。
LAN_CAPABLE_TYPES = frozenset({0, 8, 12, 23})
# 缓存里的 IP 多久之内直接试，超了就当过期（设备可能换了 IP）
LAN_CACHE_TTL = 24 * 3600


class DeviceChannel(Protocol):
    name: str

    def get_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def set_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def call_action(
        self, did: str, siid: int, aiid: int, values: list[Any]
    ) -> dict[str, Any]: ...


class CloudChannel:
    """走米家云端。"""

    name = "cloud"

    def __init__(self, api: CloudApi) -> None:
        self._api = api

    def get_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._api.get_props(params)

    def set_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._api.set_props(params)

    def call_action(
        self, did: str, siid: int, aiid: int, values: list[Any]
    ) -> dict[str, Any]:
        return self._api.call_action(did, siid, aiid, values)


class AutoChannel:
    """局域网优先，失败回落云端。

    「找得到设备」不等于「设备愿意在局域网上干活」——实测有设备握手正常、
    却对 get_properties 回 user ack timeout。所以回落必须做在每次调用上，
    只在定位阶段回落是不够的。
    """

    name = "auto"

    def __init__(self, lan: "LanChannel", cloud: CloudChannel, on_note: Any = None) -> None:
        self._lan: LanChannel | None = lan
        self._cloud = cloud
        self._on_note = on_note

    def _run(self, action: str, call):
        if self._lan is not None:
            try:
                return call(self._lan)
            except MiCliError as err:
                # 这台设备这次会话内不再试局域网，免得每条命令都白等一次
                self._lan = None
                if self._on_note:
                    self._on_note(
                        f"[dim]局域网{action}失败（{err.message}），改走云端[/dim]"
                    )
        return call(self._cloud)

    def get_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._run("读取", lambda channel: channel.get_props(params))

    def set_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._run("写入", lambda channel: channel.set_props(params))

    def call_action(
        self, did: str, siid: int, aiid: int, values: list[Any]
    ) -> dict[str, Any]:
        return self._run(
            "调用动作", lambda channel: channel.call_action(did, siid, aiid, values)
        )


class LanChannel:
    """直连设备。"""

    name = "lan"

    def __init__(self, device: LanDevice) -> None:
        self._device = device

    @property
    def endpoint(self) -> Endpoint:
        return self._device.endpoint

    def get_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._device.get_props(params)

    def set_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._device.set_props(params)

    def call_action(
        self, did: str, siid: int, aiid: int, values: list[Any]
    ) -> dict[str, Any]:
        return self._device.call_action(did, siid, aiid, values)


def lan_capable(device: Device) -> bool:
    return bool(device.token) and device.connect_type in LAN_CAPABLE_TYPES


def locate(
    profile: Profile, device: Device, *, rediscover: bool = True
) -> Endpoint | None:
    """找出设备在局域网上的地址。

    先试缓存里的 IP（一次 unicast hello，几十毫秒），不行再广播扫一遍。
    """
    cache = profile.read_lan()
    entry = cache.get(device.did)
    if entry and time.time() - entry.get("seen_at", 0) < LAN_CACHE_TTL:
        endpoint = ping(entry["ip"])
        if endpoint and endpoint.did == device.did:
            return endpoint
    if not rediscover:
        return None
    found = discover()
    if found:
        cache.update(
            {
                did: {"ip": item.ip, "seen_at": int(time.time())}
                for did, item in found.items()
            }
        )
        profile.write_lan(cache)
    return found.get(device.did)


def open_device_channel(
    session: Session,
    profile: Profile,
    device: Device,
    *,
    mode: str = "auto",
    on_note: Any = None,
) -> DeviceChannel:
    """按 --channel 选一条通道。

    auto：能直连就直连，任何一步不成立就静默回落云端——回落是常态，不该吵。
    lan：只走局域网，不成立就报错说清楚为什么。
    """
    cloud = CloudChannel(CloudApi(session))
    if mode == "cloud":
        return cloud

    if not lan_capable(device):
        reason = (
            "没有局域网 token"
            if not device.token
            else f"connect_type={device.connect_type}，不是直连路由器的设备"
            "（蓝牙 Mesh / ZigBee 要走网关）"
        )
        if mode == "lan":
            raise MiCliError(f"{device.label} 不能局域网直连：{reason}")
        return cloud

    endpoint = locate(profile, device)
    if endpoint is None:
        if mode == "lan":
            raise DeviceOffline(
                f"局域网上找不到 {device.label}",
                hint="确认在同一网段、设备在线；`mi lan discover` 看看扫得到谁",
            )
        return cloud

    if on_note:
        on_note(f"[dim]局域网直连 {endpoint.ip}（{endpoint.elapsed_ms:.0f}ms）[/dim]")
    lan = LanChannel(LanDevice(device.did, device.token or "", endpoint))
    if mode == "lan":
        return lan
    return AutoChannel(lan, cloud, on_note=on_note)
