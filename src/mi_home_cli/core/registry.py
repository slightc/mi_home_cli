"""设备清单缓存，以及「一句人话 → 具体设备」的解析。

CLI 好不好用几乎全看这里：用户不该记 did。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import AmbiguousReference, DeviceNotFound
from ..store import Profile


@dataclass
class Device:
    did: str
    name: str
    model: str
    urn: str
    online: bool
    home_id: str = ""
    home_name: str = ""
    room_id: str = ""
    room_name: str = ""
    token: str | None = None
    local_ip: str | None = None
    connect_type: int = -1
    manufacturer: str = ""
    fw_version: str | None = None
    parent_id: str | None = None
    sub_devices: dict[str, Any] = field(default_factory=dict)
    alias: str | None = None

    @property
    def label(self) -> str:
        return self.alias or self.name

    @property
    def location(self) -> str:
        if self.room_name:
            return f"{self.home_name}/{self.room_name}"
        return self.home_name or "-"

    def summary(self, *, wide: bool = False) -> dict[str, Any]:
        row: dict[str, Any] = {
            "名称": self.label,
            "家庭": self.home_name or "-",
            "房间": self.room_name or "-",
            "型号": self.model,
            "在线": "是" if self.online else "否",
        }
        if wide:
            row.update(
                {
                    "did": self.did,
                    "IP": self.local_ip or "-",
                    "固件": self.fw_version or "-",
                }
            )
        return row

    def detail(self) -> dict[str, Any]:
        data = {
            "名称": self.name,
            "别名": self.alias or "-",
            "did": self.did,
            "型号": self.model,
            "urn": self.urn,
            "厂商": self.manufacturer,
            "家庭": self.home_name or "-",
            "房间": self.room_name or "-",
            "在线": "是" if self.online else "否",
            "局域网 IP": self.local_ip or "-",
            "固件版本": self.fw_version or "-",
        }
        if self.sub_devices:
            data["子设备"] = ", ".join(self.sub_devices)
        return data

    @staticmethod
    def load(data: dict[str, Any], alias: str | None = None) -> "Device":
        return Device(
            did=data["did"],
            name=data.get("name", ""),
            model=data.get("model", ""),
            urn=data.get("urn", ""),
            online=bool(data.get("online", False)),
            home_id=str(data.get("home_id", "")),
            home_name=data.get("home_name", "") or "",
            room_id=str(data.get("room_id", "")),
            room_name=data.get("room_name", "") or "",
            token=data.get("token"),
            local_ip=data.get("local_ip"),
            connect_type=int(data.get("connect_type", -1) or -1),
            manufacturer=data.get("manufacturer", ""),
            fw_version=data.get("fw_version"),
            parent_id=data.get("parent_id"),
            sub_devices=data.get("sub_devices") or {},
            alias=alias,
        )


class Registry:
    """一个 profile 的设备清单快照。"""

    def __init__(
        self,
        data: dict[str, Any],
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.uid: str | None = data.get("uid")
        self.synced_at: int = int(data.get("synced_at", 0))
        self.homes: list[dict[str, Any]] = data.get("homes") or []
        by_did = {did: name for name, did in (aliases or {}).items()}
        self.devices: list[Device] = [
            Device.load(item, alias=by_did.get(did))
            for did, item in (data.get("devices") or {}).items()
        ]
        self.devices.sort(key=lambda d: (d.home_name, d.room_name, d.name))

    @property
    def age(self) -> int:
        return int(time.time()) - self.synced_at

    @staticmethod
    def read(profile: Profile) -> "Registry | None":
        from ..store import _read_json  # 内部工具，权限处理都在那边

        data = _read_json(profile.devices_path)
        if not data:
            return None
        return Registry(data, profile.read_aliases())

    @staticmethod
    def write(profile: Profile, data: dict[str, Any]) -> "Registry":
        payload = {**data, "synced_at": int(time.time())}
        profile.write_devices(payload)
        return Registry(payload, profile.read_aliases())

    # ---------- 过滤 ----------

    def filter(
        self,
        *,
        home: str | None = None,
        home_id: str | None = None,
        room: str | None = None,
        model: str | None = None,
        online: bool | None = None,
        search: str | None = None,
    ) -> list[Device]:
        def match(device: Device) -> bool:
            if home_id and device.home_id != home_id:
                return False
            if home and home.lower() not in device.home_name.lower():
                return False
            if room and room.lower() not in device.room_name.lower():
                return False
            if model and model.lower() not in device.model.lower():
                return False
            if online is not None and device.online != online:
                return False
            if search:
                haystack = f"{device.label} {device.name} {device.model}".lower()
                if search.lower() not in haystack:
                    return False
            return True

        return [device for device in self.devices if match(device)]

    # ---------- 解析 ----------

    def find_home(self, ref: str) -> dict[str, Any]:
        """按名称或 home_id 找一个家庭。"""
        lowered = ref.strip().lower()
        stages = [
            [h for h in self.homes if str(h["home_id"]) == ref.strip()],
            [h for h in self.homes if h["home_name"].lower() == lowered],
            [h for h in self.homes if lowered in h["home_name"].lower()],
        ]
        for candidates in stages:
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise AmbiguousReference(
                    f"`{ref}` 匹配到多个家庭："
                    + "、".join(h["home_name"] for h in candidates)
                )
        raise DeviceNotFound(
            f"没有找到家庭 `{ref}`",
            hint="用 `mi home list` 看看有哪些家庭",
        )

    def resolve(self, ref: str, **filters: Any) -> Device:
        """把用户写的东西变成一台具体设备。

        按精确度从高到低匹配，某一级命中就不再往下找；同一级命中多个则报歧义，
        并把候选列出来让用户挑。
        """
        pool = self.filter(**filters)
        if not pool:
            raise DeviceNotFound("当前过滤条件下没有任何设备")
        needle = ref.strip()
        lowered = needle.lower()

        stages: list[list[Device]] = [
            [d for d in pool if d.did == needle],
            [d for d in pool if d.alias and d.alias.lower() == lowered],
            [d for d in pool if d.name.lower() == lowered],
            [
                d
                for d in pool
                if "/" in needle
                and f"{d.room_name}/{d.name}".lower() == lowered
            ],
            [d for d in pool if lowered in d.name.lower()],
            [d for d in pool if lowered in d.model.lower()],
        ]
        for candidates in stages:
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise AmbiguousReference(
                    f"`{ref}` 匹配到 {len(candidates)} 台设备：\n"
                    + "\n".join(
                        f"  {d.location} / {d.label}（{d.model}，did {d.did}）"
                        for d in candidates[:10]
                    )
                    + ("\n  …" if len(candidates) > 10 else ""),
                    hint="用完整名称、别名或 did，也可以加 --home / --room 缩小范围",
                )
        raise DeviceNotFound(
            f"没有找到设备 `{ref}`",
            hint="用 `mi device list` 看看有哪些设备，或 `mi device sync` 刷新缓存",
        )
