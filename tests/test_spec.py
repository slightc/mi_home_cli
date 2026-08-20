"""spec 解析与取值转换。"""
import pytest

from mi_home_cli.core.spec import (
    DeviceSpec,
    format_value,
    parse_value,
    unit_symbol,
    urn_name,
)
from mi_home_cli.errors import AmbiguousReference, InvalidValue, SpecNotFound

INSTANCE = {
    "type": "urn:miot-spec-v2:device:air-purifier:0000A007:xiaomi-ma6:1",
    "description": "Air Purifier",
    "services": [
        {
            "iid": 2,
            "type": "urn:miot-spec-v2:service:air-purifier:00007811:x:1",
            "properties": [
                {
                    "iid": 1,
                    "type": "urn:miot-spec-v2:property:on:00000006:x:1",
                    "format": "bool",
                    "access": ["read", "write", "notify"],
                },
                {
                    "iid": 3,
                    "type": "urn:miot-spec-v2:property:mode:00000008:x:1",
                    "description": "Mode",
                    "format": "uint8",
                    "access": ["read", "write"],
                    "value-list": [
                        {"value": 0, "description": "Auto"},
                        {"value": 3, "description": "Sleep"},
                    ],
                },
                {
                    "iid": 4,
                    "type": "urn:miot-spec-v2:property:fault:00000009:x:1",
                    "format": "uint8",
                    "access": ["read"],
                },
            ],
            "actions": [
                {
                    "iid": 1,
                    "type": "urn:miot-spec-v2:action:toggle:00002811:x:1",
                    "description": "Toggle",
                    "in": [3],
                }
            ],
            "events": [
                {
                    "iid": 1,
                    "type": "urn:miot-spec-v2:event:low-battery:0000200x:x:1",
                    "description": "Low Battery",
                }
            ],
        },
        {
            "iid": 6,
            "type": "urn:miot-spec-v2:service:screen:00007806:x:1",
            "properties": [
                {
                    "iid": 1,
                    "type": "urn:miot-spec-v2:property:on:00000006:x:1",
                    "format": "bool",
                    "access": ["read", "write"],
                },
                {
                    "iid": 2,
                    "type": "urn:miot-spec-v2:property:brightness:0000000D:x:1",
                    "format": "uint8",
                    "access": ["read", "write"],
                    "unit": "percentage",
                    "value-range": [1, 100, 1],
                },
            ],
        },
    ],
}

TRANSLATIONS = {
    "service:002:property:003": "模式",
    "service:002:property:003:valuelist:000": "自动",
    "service:002:property:003:valuelist:001": "睡眠",
    "service:002:action:001": "切换",
}


@pytest.fixture()
def spec() -> DeviceSpec:
    return DeviceSpec(INSTANCE["type"], INSTANCE, TRANSLATIONS)


def test_urn_name():
    assert urn_name("urn:miot-spec-v2:property:brightness:0000000D:x:1") == "brightness"


def test_parses_services_properties_actions_events(spec: DeviceSpec):
    assert spec.name == "air-purifier"
    assert len(spec.properties) == 5
    assert len(spec.actions) == 1 and len(spec.events) == 1
    prop = spec.find_property("2.3")
    assert prop.full_name == "air-purifier.mode"
    assert prop.access_text() == "rw"
    assert prop.readable and prop.writable and not prop.notifiable


def test_applies_chinese_translations(spec: DeviceSpec):
    prop = spec.find_property("2.3")
    assert prop.description == "模式"
    assert [item.description for item in prop.value_list] == ["自动", "睡眠"]
    assert spec.find_action("2.1").description == "切换"


def test_find_property_by_full_name_and_bare_name(spec: DeviceSpec):
    assert spec.find_property("screen.brightness").ref == "6.2"
    assert spec.find_property("brightness").ref == "6.2"
    assert spec.find_property("模式").ref == "2.3"


def test_bare_name_prefers_primary_service(spec: DeviceSpec):
    """`on` 在主体和屏幕上都有，设备类型是 air-purifier，取主体那个。"""
    assert spec.find_property("on").ref == "2.1"
    assert spec.find_property("screen.on").ref == "6.1"


def test_ambiguous_reference_raises(spec: DeviceSpec):
    # 两个服务都叫 x 的情况：构造一个没有主服务偏好的 spec
    instance = {**INSTANCE, "type": "urn:miot-spec-v2:device:gateway:0:x:1"}
    other = DeviceSpec(instance["type"], instance, {})
    with pytest.raises(AmbiguousReference):
        other.find_property("on")


def test_unknown_property(spec: DeviceSpec):
    with pytest.raises(SpecNotFound):
        spec.find_property("nope")
    with pytest.raises(SpecNotFound):
        spec.find_property("9.9")


def test_parse_bool_accepts_common_words(spec: DeviceSpec):
    prop = spec.find_property("2.1")
    for word in ("on", "true", "1", "开", "打开"):
        assert parse_value(prop, word) is True
    for word in ("off", "false", "0", "关"):
        assert parse_value(prop, word) is False
    with pytest.raises(InvalidValue):
        parse_value(prop, "maybe")


def test_parse_enum_by_description_or_value(spec: DeviceSpec):
    prop = spec.find_property("2.3")
    assert parse_value(prop, "睡眠") == 3
    assert parse_value(prop, "Sleep") == 3  # 英文原文也认
    assert parse_value(prop, "3") == 3
    with pytest.raises(InvalidValue):
        parse_value(prop, "狂暴")


def test_parse_number_checks_range(spec: DeviceSpec):
    prop = spec.find_property("screen.brightness")
    assert parse_value(prop, "60") == 60
    with pytest.raises(InvalidValue):
        parse_value(prop, "300")
    with pytest.raises(InvalidValue):
        parse_value(prop, "abc")


def test_format_value(spec: DeviceSpec):
    assert format_value(spec.find_property("2.1"), True) == "开"
    assert format_value(spec.find_property("2.3"), 3) == "睡眠(3)"
    assert format_value(spec.find_property("screen.brightness"), 60) == "60%"
    assert format_value(spec.find_property("2.4"), None) == "-"


def test_unit_symbol():
    assert unit_symbol("percentage") == "%"
    assert unit_symbol("kelvin") == "K"
    assert unit_symbol("none") == ""
    assert unit_symbol(None) == ""
    assert unit_symbol("怪单位") == "怪单位"


def test_range_text(spec: DeviceSpec):
    assert spec.find_property("screen.brightness").range_text() == "1~100 step 1"
    assert spec.find_property("2.3").range_text() == "0=自动 3=睡眠"
    assert spec.find_property("2.1").range_text() == "-"
