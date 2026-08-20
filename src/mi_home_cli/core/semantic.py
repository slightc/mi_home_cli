"""语义命令用的映射：`--brightness 60` → 到底写哪个属性。

能这么做是因为 MIoT spec 里属性的 urn 名是标准化的（on、brightness、
color-temperature、target-temperature、fan-level、motor-control…），
不同厂商的同类设备用的是同一套名字。所以语义层只认名字，不认型号。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..errors import InvalidValue, SpecNotFound
from .spec import DeviceSpec, Property, parse_value

# 常见颜色的中英文别名 → RGB
NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0), "红": (255, 0, 0), "红色": (255, 0, 0),
    "green": (0, 255, 0), "绿": (0, 255, 0), "绿色": (0, 255, 0),
    "blue": (0, 0, 255), "蓝": (0, 0, 255), "蓝色": (0, 0, 255),
    "white": (255, 255, 255), "白": (255, 255, 255), "白色": (255, 255, 255),
    "yellow": (255, 255, 0), "黄": (255, 255, 0), "黄色": (255, 255, 0),
    "orange": (255, 165, 0), "橙": (255, 165, 0), "橙色": (255, 165, 0),
    "purple": (128, 0, 128), "紫": (128, 0, 128), "紫色": (128, 0, 128),
    "pink": (255, 105, 180), "粉": (255, 105, 180), "粉色": (255, 105, 180),
    "cyan": (0, 255, 255), "青": (0, 255, 255), "青色": (0, 255, 255),
    "warm": (255, 170, 100), "暖": (255, 170, 100), "暖色": (255, 170, 100),
}

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(text: str) -> int:
    """`#ff8800` / `ff8800` / `红` / `red` → spec 用的 uint32 RGB。"""
    value = text.strip()
    match = _HEX_RE.match(value)
    if match:
        red, green, blue = (int(match[1][i:i + 2], 16) for i in (0, 2, 4))
    elif value.lower() in NAMED_COLORS:
        red, green, blue = NAMED_COLORS[value.lower()]
    else:
        raise InvalidValue(
            f"看不懂的颜色 `{text}`",
            hint="写 #ff8800 这样的十六进制，或 " + "、".join(
                list(NAMED_COLORS)[:6]
            ),
        )
    return (red << 16) | (green << 8) | blue


def format_color(value: Any) -> str:
    if not isinstance(value, int):
        return str(value)
    return f"#{value & 0xFFFFFF:06x}"


def enum_value(prop: Property, *keywords: str) -> Any | None:
    """在枚举里找语义匹配的值，比如窗帘的「打开」。"""
    for item in prop.value_list:
        text = f"{item.description} {item.raw}".lower()
        if any(keyword.lower() in text for keyword in keywords):
            return item.value
    return None


@dataclass
class Change:
    """一次待下发的属性写入。"""

    prop: Property
    value: Any

    def param(self, did: str) -> dict[str, Any]:
        return {
            "did": did,
            "siid": self.prop.siid,
            "piid": self.prop.piid,
            "value": self.value,
        }


class Planner:
    """把语义选项翻成一组属性写入。

    找不到对应属性时直接报错，并说清楚这台设备支持什么——比默默忽略强。
    """

    def __init__(self, spec: DeviceSpec, label: str) -> None:
        self.spec = spec
        self.label = label
        self.changes: list[Change] = []

    def _require(self, option: str, *names: str) -> Property:
        prop = self.spec.by_name(*names, writable=True)
        if prop is None:
            raise SpecNotFound(
                f"{self.label} 不支持 {option}（spec 里没有可写的 "
                f"{' / '.join(names)}）",
                hint=f"用 `mi spec show {self.label} --writable` 看看支持什么",
            )
        return prop

    def add_raw(self, option: str, names: tuple[str, ...], value: Any) -> None:
        self.changes.append(Change(self._require(option, *names), value))

    def add_parsed(self, option: str, names: tuple[str, ...], raw: str) -> None:
        prop = self._require(option, *names)
        self.changes.append(Change(prop, parse_value(prop, raw)))

    def add_enum(
        self, option: str, names: tuple[str, ...], *keywords: str
    ) -> None:
        prop = self._require(option, *names)
        value = enum_value(prop, *keywords)
        if value is None:
            raise SpecNotFound(
                f"{self.label} 的 {prop.full_name} 里没有「{keywords[0]}」这一档",
                hint="可选值：" + prop.range_text(),
            )
        self.changes.append(Change(prop, value))

    def params(self, did: str) -> list[dict[str, Any]]:
        # on 放最前面：先通电再调参数，多数设备更听话
        ordered = sorted(self.changes, key=lambda c: c.prop.name != "on")
        return [change.param(did) for change in ordered]


# 各类设备「看状态」时值得显示的属性，按这个顺序
STATUS_PROPERTIES: dict[str, tuple[str, ...]] = {
    "light": ("on", "brightness", "color-temperature", "color", "mode"),
    "climate": (
        "on", "mode", "target-temperature", "temperature",
        "relative-humidity", "fan-level", "status",
    ),
    "cover": ("motor-control", "status", "current-position", "target-position"),
    "fan": ("on", "fan-level", "speed-level", "mode", "horizontal-swing", "status"),
}
