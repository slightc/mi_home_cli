"""米家云端接口：家庭、房间、设备清单，以及属性读写。

接口路径与字段来自对 Home Assistant 米家集成公开行为的观察，不是小米对外承诺
的契约。所有 URL 都集中在这里，接口有变只改这一个文件。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from ..errors import CloudError
from .session import Session

# 单次 device_list_page 请求最多带多少个 did
DIDS_PER_REQUEST = 150
# 单次 prop/get 最多读多少条属性
PROPS_PER_REQUEST = 150

# 这些设备定义了 spec 但实际不可用，跟上游一样直接跳过
_IGNORED_DID_PREFIXES = ("miwifi.",)
# did 形如 600123456.s2 的是子设备
_SUB_DEVICE_RE = re.compile(r"\.s\d+$")


def _chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


class CloudApi:
    """一层薄封装，只负责把云端的数据结构整理成好用的形状。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------- 家庭 / 房间 ----------

    def fetch_homes(self) -> dict[str, Any]:
        """家庭、房间，以及每个房间下的 did 列表。"""
        res = self.session.api_post(
            "/app/v2/homeroom/gethome",
            {
                "limit": 150,
                "fetch_share": True,
                "fetch_share_dev": True,
                "plat_form": 0,
                "app_ver": 9,
            },
        ).get("result")
        if not isinstance(res, dict):
            raise CloudError("gethome 响应缺少 result")

        uid: str | None = None
        homes: list[dict[str, Any]] = []
        for source, shared in (("homelist", False), ("share_home_list", True)):
            for home in res.get(source) or []:
                if "id" not in home or "name" not in home:
                    continue
                if uid is None and not shared and home.get("uid"):
                    uid = str(home["uid"])
                rooms = [
                    {
                        "room_id": str(room["id"]),
                        "room_name": room.get("name", ""),
                        "dids": room.get("dids") or [],
                    }
                    for room in home.get("roomlist") or []
                    if "id" in room
                ]
                homes.append(
                    {
                        "home_id": str(home["id"]),
                        "home_name": home["name"],
                        "uid": str(home.get("uid") or ""),
                        "shared": shared,
                        "address": home.get("address"),
                        # 不属于任何房间的设备挂在家庭本身下面
                        "dids": home.get("dids") or [],
                        "rooms": rooms,
                    }
                )
        return {"uid": uid, "homes": homes}

    # ---------- 设备 ----------

    def _device_list_page(
        self, dids: list[str], start_did: str | None = None
    ) -> dict[str, dict[str, Any]]:
        data: dict[str, Any] = {
            "limit": 200,
            "get_split_device": True,
            "get_third_device": True,
            "dids": dids,
        }
        if start_did:
            data["start_did"] = start_did
        res = self.session.api_post("/app/v2/home/device_list_page", data).get(
            "result"
        )
        if not isinstance(res, dict):
            raise CloudError("device_list_page 响应缺少 result")

        devices: dict[str, dict[str, Any]] = {}
        for item in res.get("list") or []:
            did, name = item.get("did"), item.get("name")
            model, urn = item.get("model"), item.get("spec_type")
            if not did or not name or not model or not urn:
                continue
            if did.startswith(_IGNORED_DID_PREFIXES):
                continue
            devices[did] = {
                "did": did,
                "name": name,
                "model": model,
                "urn": urn,
                "manufacturer": model.split(".")[0],
                "online": bool(item.get("isOnline", False)),
                "token": item.get("token"),
                "local_ip": item.get("local_ip"),
                "ssid": item.get("ssid"),
                "rssi": item.get("rssi"),
                "connect_type": item.get("pid", -1),
                "parent_id": item.get("parent_id"),
                "owner": item.get("owner"),
                "fw_version": (item.get("extra") or {}).get("fw_version"),
            }

        next_start = res.get("next_start_did")
        if res.get("has_more") and next_start:
            devices.update(self._device_list_page(dids, start_did=next_start))
        return devices

    def fetch_device_details(self, dids: list[str]) -> dict[str, dict[str, Any]]:
        devices: dict[str, dict[str, Any]] = {}
        for chunk in _chunked(dids, DIDS_PER_REQUEST):
            devices.update(self._device_list_page(chunk))
        return devices

    def fetch_shared_devices(self) -> dict[str, dict[str, Any]]:
        """被单独分享过来、不属于任何家庭的设备。"""
        result: dict[str, dict[str, Any]] = {}
        for did, device in self._device_list_page([]).items():
            owner = device.get("owner")
            if isinstance(owner, dict) and "userid" in owner:
                result[did] = device
        return result

    def fetch_all(self) -> dict[str, Any]:
        """一次拉齐：家庭、房间、设备，并把归属信息合进设备。"""
        home_info = self.fetch_homes()
        placement: dict[str, dict[str, Any]] = {}
        for home in home_info["homes"]:
            base = {
                "home_id": home["home_id"],
                "home_name": home["home_name"],
                "room_id": "",
                "room_name": "",
            }
            for did in home["dids"]:
                placement[did] = dict(base)
            for room in home["rooms"]:
                for did in room["dids"]:
                    placement[did] = {
                        **base,
                        "room_id": room["room_id"],
                        "room_name": room["room_name"],
                    }

        devices = self.fetch_device_details(sorted(placement))
        for did, info in devices.items():
            info.update(placement.get(did, {}))

        shared = self.fetch_shared_devices()
        homes = list(home_info["homes"])
        if shared:
            for did, info in shared.items():
                if did in devices:
                    continue
                owner = info.get("owner") or {}
                owner_name = owner.get("nickname") or "共享设备"
                info.update(
                    {
                        "home_id": f"shared:{owner.get('userid', '')}",
                        "home_name": owner_name,
                        "room_id": "",
                        "room_name": "",
                    }
                )
                devices[did] = info
            owners = {
                info["home_id"]: info["home_name"]
                for info in devices.values()
                if str(info.get("home_id", "")).startswith("shared:")
            }
            homes.extend(
                {
                    "home_id": home_id,
                    "home_name": name,
                    "uid": home_id.split(":", 1)[1],
                    "shared": True,
                    "address": None,
                    "dids": [],
                    "rooms": [],
                }
                for home_id, name in sorted(owners.items())
            )

        # 子设备（did 带 .sN 后缀）挂到父设备下面，列表里不单独出现
        for did in list(devices):
            match = _SUB_DEVICE_RE.search(did)
            if not match:
                continue
            parent_did = did[: match.start()]
            if parent_did in devices:
                child = devices.pop(did)
                devices[parent_did].setdefault("sub_devices", {})[
                    match.group()[1:]
                ] = child

        return {"uid": home_info["uid"], "homes": homes, "devices": devices}

    # ---------- 属性 / 动作 ----------

    def get_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """params: [{"did":..., "siid":..., "piid":...}, ...]"""
        results: list[dict[str, Any]] = []
        for index in range(0, len(params), PROPS_PER_REQUEST):
            chunk = params[index:index + PROPS_PER_REQUEST]
            res = self.session.api_post(
                "/app/v2/miotspec/prop/get",
                {"datasource": 1, "params": chunk},
            )
            results.extend(res.get("result") or [])
        return results

    def set_props(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """params: [{"did":..., "siid":..., "piid":..., "value":...}, ...]"""
        res = self.session.api_post(
            "/app/v2/miotspec/prop/set", {"params": params}
        )
        return res.get("result") or []

    def call_action(
        self, did: str, siid: int, aiid: int, values: list[Any]
    ) -> dict[str, Any]:
        # 注意：这个接口的 in 是「值的数组」，不是 {piid, value} 的数组
        res = self.session.api_post(
            "/app/v2/miotspec/action",
            {"params": {"did": did, "siid": siid, "aiid": aiid, "in": values}},
        )
        return res.get("result") or {}
