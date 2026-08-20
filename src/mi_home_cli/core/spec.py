"""MIoT spec：设备能力描述的获取、缓存与解析。

spec 决定了每台设备有哪些属性、属性在哪个 siid.piid、取值范围和枚举是什么。
数据来自 miot-spec.org 的公开接口，不需要登录。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..errors import AmbiguousReference, InvalidValue, NetworkError, SpecNotFound
from ..store import Profile

INSTANCE_URL = "https://miot-spec.org/miot-spec-v2/instance"
MULTI_LANG_URL = "https://miot-spec.org/instance/v2/multiLanguage"

# spec 基本不变，缓存久一点；和上游一样 14 天
SPEC_TTL = 14 * 24 * 3600
DEFAULT_LANG = "zh_cn"

# spec 里的单位是英文全称，显示时换成常见符号
_UNIT_SYMBOLS = {
    "percentage": "%",
    "kelvin": "K",
    "celsius": "℃",
    "fahrenheit": "℉",
    "seconds": "s",
    "minutes": "min",
    "hours": "h",
    "days": "d",
    "watt": "W",
    "kwh": "kWh",
    "ppm": "ppm",
    "μg/m3": "μg/m³",
    "ug/m3": "μg/m³",
    "mg/m3": "mg/m³",
    "lux": "lx",
    "pascal": "Pa",
    "arcdegrees": "°",
    "rgb": "",
    "none": "",
}

_TRUE_WORDS = {"true", "on", "1", "yes", "y", "开", "是", "打开"}
_FALSE_WORDS = {"false", "off", "0", "no", "n", "关", "否", "关闭"}


def urn_name(type_urn: str) -> str:
    """从 urn 里取出可读名，如 ...:property:brightness:... → brightness。"""
    parts = type_urn.split(":")
    return parts[3] if len(parts) > 3 else type_urn


@dataclass
class ValueItem:
    value: Any
    description: str
    # spec 原文（英文）。翻译过之后 description 是中文，但用户照着 spec 文档
    # 写英文名也应该认，所以两个都留着。
    raw: str = ""

    def matches(self, text: str) -> bool:
        lowered = text.lower()
        return lowered in (
            str(self.description).lower(),
            str(self.raw).lower(),
        )

    def __str__(self) -> str:
        return f"{self.value}={self.description}"


@dataclass
class Property:
    siid: int
    piid: int
    name: str
    description: str
    format: str
    access: list[str]
    service: str
    unit: str | None = None
    value_range: list[Any] | None = None
    value_list: list[ValueItem] = field(default_factory=list)

    @property
    def readable(self) -> bool:
        return "read" in self.access

    @property
    def writable(self) -> bool:
        return "write" in self.access

    @property
    def notifiable(self) -> bool:
        return "notify" in self.access

    @property
    def ref(self) -> str:
        return f"{self.siid}.{self.piid}"

    @property
    def full_name(self) -> str:
        return f"{self.service}.{self.name}"

    def access_text(self) -> str:
        return "".join(
            flag
            for flag, ok in (
                ("r", self.readable), ("w", self.writable), ("n", self.notifiable)
            )
            if ok
        )

    def range_text(self) -> str:
        if self.value_list:
            return " ".join(str(item) for item in self.value_list)
        if self.value_range and len(self.value_range) >= 2:
            text = f"{self.value_range[0]}~{self.value_range[1]}"
            if len(self.value_range) > 2:
                text += f" step {self.value_range[2]}"
            return text
        return "-"


@dataclass
class Action:
    siid: int
    aiid: int
    name: str
    description: str
    service: str
    in_piids: list[int] = field(default_factory=list)
    out_piids: list[int] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.siid}.{self.aiid}"

    @property
    def full_name(self) -> str:
        return f"{self.service}.{self.name}"


@dataclass
class Event:
    siid: int
    eiid: int
    name: str
    description: str
    service: str

    @property
    def full_name(self) -> str:
        return f"{self.service}.{self.name}"


class DeviceSpec:
    """一台设备（准确说是一个 urn）的能力描述。"""

    def __init__(self, urn: str, instance: dict[str, Any], translations: dict[str, str]):
        self.urn = urn
        self.description: str = instance.get("description", "")
        self.name: str = urn_name(instance.get("type", urn))
        self.properties: list[Property] = []
        self.actions: list[Action] = []
        self.events: list[Event] = []
        self._parse(instance, translations)

    def _parse(self, instance: dict[str, Any], trans: dict[str, str]) -> None:
        for service in instance.get("services") or []:
            siid = service.get("iid")
            if siid is None:
                continue
            skey = f"service:{siid:03d}"
            service_name = urn_name(service.get("type", ""))
            for prop in service.get("properties") or []:
                piid = prop.get("iid")
                if piid is None:
                    continue
                pkey = f"{skey}:property:{piid:03d}"
                value_list = [
                    ValueItem(
                        value=item.get("value"),
                        description=trans.get(
                            f"{pkey}:valuelist:{index:03d}",
                            item.get("description", ""),
                        )
                        or str(item.get("value")),
                        raw=item.get("description", "") or "",
                    )
                    for index, item in enumerate(prop.get("value-list") or [])
                ]
                self.properties.append(
                    Property(
                        siid=siid,
                        piid=piid,
                        name=urn_name(prop.get("type", "")),
                        description=trans.get(pkey, prop.get("description", "")),
                        format=prop.get("format", "string"),
                        access=list(prop.get("access") or []),
                        service=service_name,
                        unit=prop.get("unit"),
                        value_range=prop.get("value-range"),
                        value_list=value_list,
                    )
                )
            for action in service.get("actions") or []:
                aiid = action.get("iid")
                if aiid is None:
                    continue
                self.actions.append(
                    Action(
                        siid=siid,
                        aiid=aiid,
                        name=urn_name(action.get("type", "")),
                        description=trans.get(
                            f"{skey}:action:{aiid:03d}",
                            action.get("description", ""),
                        ),
                        service=service_name,
                        in_piids=list(action.get("in") or []),
                        out_piids=list(action.get("out") or []),
                    )
                )
            for event in service.get("events") or []:
                eiid = event.get("iid")
                if eiid is None:
                    continue
                self.events.append(
                    Event(
                        siid=siid,
                        eiid=eiid,
                        name=urn_name(event.get("type", "")),
                        description=trans.get(
                            f"{skey}:event:{eiid:03d}", event.get("description", "")
                        ),
                        service=service_name,
                    )
                )

    # ---------- 查找 ----------

    def _prefer_primary(self, candidates: list) -> list:
        """多个同名候选时，优先设备主服务下的那个。

        比如空气净化器的 `on` 同时存在于 air-purifier 和 screen 两个服务，
        设备类型是 air-purifier，那 `on` 显然指主体开关而不是屏幕。
        """
        if len(candidates) <= 1:
            return candidates
        primary = [item for item in candidates if item.service == self.name]
        return primary if len(primary) == 1 else candidates

    def by_name(
        self,
        *names: str,
        writable: bool | None = None,
        readable: bool | None = None,
    ) -> Property | None:
        """按 spec 里的标准属性名找属性，找不到返回 None。

        属性的 urn 名（on / brightness / color-temperature …）是 MIoT 规范
        统一的，语义命令就靠它跨型号工作。names 按优先级给，先命中先用。
        """
        for name in names:
            candidates = [
                prop
                for prop in self.properties
                if prop.name == name
                and (writable is None or prop.writable == writable)
                and (readable is None or prop.readable == readable)
            ]
            candidates = self._prefer_primary(candidates)
            if candidates:
                return min(candidates, key=lambda p: (p.siid, p.piid))
        return None

    def property_at(self, siid: int, piid: int) -> Property | None:
        for prop in self.properties:
            if prop.siid == siid and prop.piid == piid:
                return prop
        return None

    def find_property(self, ref: str) -> Property:
        """支持 `2.1`、`light.brightness`、`brightness` 三种写法。"""
        ref = ref.strip()
        numeric = re.fullmatch(r"(\d+)\.(\d+)", ref)
        if numeric:
            prop = self.property_at(int(numeric[1]), int(numeric[2]))
            if prop is None:
                raise SpecNotFound(f"这台设备没有属性 {ref}")
            return prop
        lowered = ref.lower()
        stages = [
            [p for p in self.properties if p.full_name.lower() == lowered],
            [p for p in self.properties if p.name.lower() == lowered],
            [p for p in self.properties if p.description.lower() == lowered],
            [p for p in self.properties if lowered in p.description.lower()],
        ]
        for candidates in stages:
            candidates = self._prefer_primary(candidates)
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise AmbiguousReference(
                    f"`{ref}` 匹配到多个属性：\n"
                    + "\n".join(
                        f"  {p.full_name}（{p.ref}，{p.description}）"
                        for p in candidates[:10]
                    ),
                    hint="用 service.property 全名或 siid.piid",
                )
        raise SpecNotFound(
            f"这台设备没有属性 `{ref}`",
            hint="用 `mi spec show <设备>` 看看有哪些属性",
        )

    def find_action(self, ref: str) -> Action:
        ref = ref.strip()
        numeric = re.fullmatch(r"(\d+)\.(\d+)", ref)
        if numeric:
            siid, aiid = int(numeric[1]), int(numeric[2])
            for action in self.actions:
                if action.siid == siid and action.aiid == aiid:
                    return action
            raise SpecNotFound(f"这台设备没有动作 {ref}")
        lowered = ref.lower()
        stages = [
            [a for a in self.actions if a.full_name.lower() == lowered],
            [a for a in self.actions if a.name.lower() == lowered],
            [a for a in self.actions if a.description.lower() == lowered],
            [a for a in self.actions if lowered in a.description.lower()],
        ]
        for candidates in stages:
            candidates = self._prefer_primary(candidates)
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise AmbiguousReference(
                    f"`{ref}` 匹配到多个动作：\n"
                    + "\n".join(
                        f"  {a.full_name}（{a.ref}，{a.description}）"
                        for a in candidates[:10]
                    ),
                    hint="用 service.action 全名或 siid.aiid",
                )
        raise SpecNotFound(
            f"这台设备没有动作 `{ref}`",
            hint="用 `mi action list <设备>` 看看有哪些动作",
        )


# ---------- 取值转换 ----------


def parse_value(prop: Property, raw: str) -> Any:
    """把命令行上的字符串按 spec 转成接口要的类型。"""
    text = raw.strip()
    if prop.format == "bool":
        if text.lower() in _TRUE_WORDS:
            return True
        if text.lower() in _FALSE_WORDS:
            return False
        raise InvalidValue(
            f"{prop.full_name} 是开关量，`{raw}` 不认识",
            hint="写 on/off、true/false、1/0 都行",
        )
    if prop.value_list:
        for item in prop.value_list:
            if item.matches(text):
                return item.value
        for item in prop.value_list:
            if text == str(item.value):
                return item.value
        raise InvalidValue(
            f"{prop.full_name} 不接受 `{raw}`",
            hint="可选值：" + prop.range_text(),
        )
    if prop.format.startswith(("uint", "int")):
        try:
            value: Any = int(text, 0)
        except ValueError as err:
            raise InvalidValue(f"{prop.full_name} 要整数，给的是 `{raw}`") from err
    elif prop.format == "float":
        try:
            value = float(text)
        except ValueError as err:
            raise InvalidValue(f"{prop.full_name} 要数字，给的是 `{raw}`") from err
    else:
        return text

    if prop.value_range and len(prop.value_range) >= 2:
        low, high = prop.value_range[0], prop.value_range[1]
        if value < low or value > high:
            raise InvalidValue(
                f"{prop.full_name} 的取值范围是 {low}~{high}，给的是 {value}"
            )
    return value


def format_value(prop: Property, value: Any) -> str:
    """把接口返回的值变成人看的样子。

    浮点数按 6 位有效数字显示：设备回的温度是 23.700001，原样甩给用户既难看
    又像是精度错觉。用有效数字而不是固定小数位，是为了不把 0.019 mg/m³ 这种
    小量级的值四舍五入成 0.02。
    """
    if value is None:
        return "-"
    if prop.format == "bool":
        return "开" if value else "关"
    for item in prop.value_list:
        if item.value == value:
            return f"{item.description}({value})"
    if isinstance(value, float):
        value = f"{value:.6g}"
    unit = unit_symbol(prop.unit)
    if unit:
        return f"{value}{'' if unit in ('%', '°', '℃', '℉') else ' '}{unit}"
    return str(value)


def unit_symbol(unit: str | None) -> str:
    if not unit:
        return ""
    return _UNIT_SYMBOLS.get(unit.lower(), unit)


# ---------- 获取与缓存 ----------


class SpecStore:
    """spec 的本地缓存。"""

    def __init__(
        self,
        profile: Profile,
        *,
        lang: str = DEFAULT_LANG,
        ttl: int = SPEC_TTL,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.profile = profile
        self.lang = lang
        self.ttl = ttl
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._memo: dict[str, DeviceSpec] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SpecStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _cache_path(self, urn: str):
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", urn)
        return self.profile.spec_dir / f"{safe}.json"

    def _fetch(self, urn: str) -> dict[str, Any]:
        try:
            instance = self._client.get(INSTANCE_URL, params={"type": urn}).json()
        except (httpx.HTTPError, ValueError) as err:
            raise NetworkError(f"获取 spec 失败（{urn}）：{err}") from err
        if not isinstance(instance, dict) or "services" not in instance:
            raise SpecNotFound(f"miot-spec.org 没有返回有效的 spec：{urn}")
        translations: dict[str, Any] = {}
        try:
            data = self._client.get(MULTI_LANG_URL, params={"urn": urn}).json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                translations = data["data"]
        except (httpx.HTTPError, ValueError):
            # 没有翻译不影响用，descriptions 退回英文
            translations = {}
        return {
            "urn": urn,
            "fetched_at": int(time.time()),
            "instance": instance,
            "translations": translations,
        }

    def get(self, urn: str, *, refresh: bool = False) -> DeviceSpec:
        if not refresh and urn in self._memo:
            return self._memo[urn]
        path = self._cache_path(urn)
        payload: dict[str, Any] | None = None
        if not refresh and path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if payload and int(time.time()) - payload.get("fetched_at", 0) > self.ttl:
                payload = None
        if payload is None:
            payload = self._fetch(urn)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        translations = (payload.get("translations") or {}).get(self.lang) or {}
        spec = DeviceSpec(urn, payload["instance"], translations)
        self._memo[urn] = spec
        return spec
